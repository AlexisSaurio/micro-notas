import os
import time
import requests
import boto3
from decimal import Decimal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from prometheus_fastapi_instrumentator import Instrumentator
from dotenv import load_dotenv
from fpdf import FPDF

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "local")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BUCKET_NOMBRE = os.getenv("BUCKET_NOMBRE")
URL_NOTIFICACIONES = os.getenv("URL_NOTIFICACIONES", "http://localhost:8002")

dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
s3 = boto3.client('s3', region_name=AWS_REGION)

tabla_notas = dynamodb.Table(os.getenv("TABLA_NOTAS", "NotasDeVenta"))
tabla_clientes = dynamodb.Table(os.getenv("TABLA_CLIENTES", "Clientes"))
tabla_productos = dynamodb.Table(os.getenv("TABLA_PRODUCTOS", "Productos"))

app = FastAPI(title="Microservicio Notas de Venta", description=f"Ambiente actual: {APP_ENV}")

Instrumentator().instrument(app).expose(app)

def parse_decimals(obj):
    if isinstance(obj, list): return [parse_decimals(i) for i in obj]
    elif isinstance(obj, dict): return {k: parse_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal): return float(obj)
    return obj

def generar_y_subir_pdf(nota, cliente):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="NOTA DE VENTA", ln=True, align='C')
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=f"Cliente: {cliente['Nombre']}", ln=True)
    pdf.cell(200, 10, txt=f"RFC: {cliente['RFC']}", ln=True)
    pdf.cell(200, 10, txt=f"Total: ${nota['TotalGeneral']}", ln=True)
    
    for prod in nota['DetalleProductos']:
        pdf.cell(200, 10, txt=f"{prod['NombreProducto']} x{prod['Cantidad']} - ${prod['Importe']}", ln=True)
        
    ruta_temporal = f"/tmp_{nota['ID']}.pdf" 
    pdf.output(ruta_temporal)
    key_s3 = f"{cliente['RFC']}/{nota['ID']}.pdf"
    
    with open(ruta_temporal, "rb") as f:
        s3.upload_fileobj(f, BUCKET_NOMBRE, key_s3, ExtraArgs={
            'Metadata': {'hora-envio': str(time.time()), 'nota-descargada': 'false', 'veces-enviado': '1'}
        })
    
    os.remove(ruta_temporal) 
    return key_s3

@app.post("/notas", status_code=201)
async def crear_nota_venta(request: Request):
    try:
        body = await request.json()
        cliente_id = body.get('ClienteID')
        if not cliente_id: raise HTTPException(status_code=400, detail="Falta el ClienteID")
            
        resp_cliente = tabla_clientes.get_item(Key={'ID': cliente_id})
        if 'Item' not in resp_cliente: raise HTTPException(status_code=404, detail="Cliente no existe")
            
        datos_cliente = resp_cliente['Item']
        lista_final = []
        total = 0
        
        productos = body.get('Productos', [])
        if not productos: raise HTTPException(status_code=400, detail="La nota debe tener productos")
            
        for item in productos:
            producto_id = item.get('ProductoID')
            cantidad = item.get('Cantidad', 1)
            resp_prod = tabla_productos.get_item(Key={'ID': producto_id})
            
            if 'Item' not in resp_prod: raise HTTPException(status_code=404, detail=f"Producto {producto_id} no existe")
                
            info_prod = resp_prod['Item']
            precio_real = info_prod['Precio Base']
            importe = cantidad * precio_real
            total += importe
            
            lista_final.append({
                'ProductoID': producto_id,
                'NombreProducto': info_prod['Nombre'],
                'Cantidad': cantidad,
                'PrecioUnitario': precio_real,
                'Importe': importe
            })
            
        folio = int(time.time())
        nueva_nota = {
            'ID': folio,
            'Cliente': {'Nombre': datos_cliente['Nombre'], 'RFC': datos_cliente['RFC']},
            'DetalleProductos': lista_final,
            'TotalGeneral': total
        }
        
        tabla_notas.put_item(Item=nueva_nota)
        generar_y_subir_pdf(nueva_nota, nueva_nota['Cliente'])
        
        url_descarga = f"http://54.210.188.228:8001/notas/descargar?folio={folio}"
        payload_correo = {
            "cliente_nombre": datos_cliente['Nombre'],
            "folio": folio,
            "total": float(total),
            "url_descarga": url_descarga
        }
        
        try:
            requests.post(f"{URL_NOTIFICACIONES}/enviar-correo", json=payload_correo, timeout=3)
        except:
            pass # Ignoramos si falla localmente por ahora
        
        return {'mensaje': 'Venta procesada exitosamente', 'folio': folio, 'ambiente': APP_ENV}
        
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/notas")
def obtener_notas():
    try:
        respuesta = tabla_notas.scan()
        notas = respuesta.get('Items', [])
        
        for nota in notas:
            rfc_cliente = nota.get('Cliente', {}).get('RFC')
            folio_nota = nota.get('ID')
            
            if rfc_cliente and folio_nota:
                key_s3 = f"{rfc_cliente}/{folio_nota}.pdf"
                try:
                    resp_s3 = s3.head_object(Bucket=BUCKET_NOMBRE, Key=key_s3)
                    nota['Metadatos_PDF'] = resp_s3.get('Metadata', {})
                except:
                    nota['Metadatos_PDF'] = {"aviso": "PDF no encontrado en S3"}
                    
        return parse_decimals(notas)
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/notas/descargar")
def descargar_nota(folio: int):
    try:
        resp_nota = tabla_notas.get_item(Key={'ID': folio})
        if 'Item' not in resp_nota: raise HTTPException(status_code=404, detail="Folio no encontrado")

        rfc = resp_nota['Item']['Cliente']['RFC']
        key_s3 = f"{rfc}/{folio}.pdf"

        resp_s3 = s3.head_object(Bucket=BUCKET_NOMBRE, Key=key_s3)
        metas = resp_s3.get('Metadata', {})
        metas['nota-descargada'] = 'true'
        
        s3.copy_object(
            Bucket=BUCKET_NOMBRE, Key=key_s3,
            CopySource={'Bucket': BUCKET_NOMBRE, 'Key': key_s3},
            Metadata=metas, MetadataDirective='REPLACE'
        )

        url_s3 = s3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NOMBRE, 'Key': key_s3}, ExpiresIn=300)
        return RedirectResponse(url=url_s3, status_code=302)
    except Exception as e: 
        raise HTTPException(status_code=500, detail=str(e))
