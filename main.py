"""
API FastAPI para el Módulo de Encuestas
Proyecto: WFSA App
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.cloud import bigquery
from datetime import datetime, timedelta
import uvicorn
import os
import uuid
import json
import google.auth.transport.requests
import requests
import pytz

# ============================================
# CONFIGURACIÓN
# ============================================

PROJECT_ID = "worldwide-470917"
DATASET = "app_clientes"

# Tablas
TABLE_USUARIOS = f"{PROJECT_ID}.{DATASET}.usuarios_app"
TABLE_USUARIO_INST = f"{PROJECT_ID}.{DATASET}.usuario_instalaciones"
TABLE_ENCUESTAS_CONFIG = f"{PROJECT_ID}.{DATASET}.encuestas_configuracion"
TABLE_ENCUESTAS_PREGUNTAS = f"{PROJECT_ID}.{DATASET}.encuestas_preguntas"
TABLE_ENCUESTAS_SOLICITUDES = f"{PROJECT_ID}.{DATASET}.encuestas_solicitudes"
TABLE_ENCUESTAS_RESPUESTAS = f"{PROJECT_ID}.{DATASET}.encuestas_respuestas"
TABLE_ENCUESTAS_NOTIF_PROG = f"{PROJECT_ID}.{DATASET}.encuestas_notificaciones_programadas"
TABLE_ENCUESTAS_NOTIF_LOG = f"{PROJECT_ID}.{DATASET}.encuestas_notificaciones_log"

# Cliente BigQuery
client = bigquery.Client(project=PROJECT_ID)

# ============================================
# CREAR APLICACIÓN
# ============================================

app = FastAPI(
    title="WFSA Encuestas API",
    description="API para gestión de encuestas de satisfacción",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# ENDPOINTS BÁSICOS
# ============================================

@app.get("/")
async def root():
    return {
        "service": "WFSA Encuestas API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# ============================================
# ENDPOINT 1: GENERAR ENCUESTAS MENSUALES
# ============================================

@app.post("/api/encuestas/generar-mensuales")
async def generar_encuestas_mensuales():
    """
    Genera encuestas mensuales para todas las instalaciones.
    Se ejecuta automáticamente vía Cloud Scheduler el día 1 de cada mes.
    """
    try:
        # 1. Obtener configuración activa
        query_config = f"""
        SELECT *
        FROM `{TABLE_ENCUESTAS_CONFIG}`
        WHERE activo = TRUE
        LIMIT 1
        """
        config_result = list(client.query(query_config).result())
        if not config_result:
            raise HTTPException(status_code=400, detail="No hay configuración activa")
        
        config = dict(config_result[0])
        
        # 2. Periodo actual
        ahora = datetime.now()
        periodo = ahora.strftime("%Y%m")
        
        # 3. Fecha límite
        dias_responder = config['dias_para_responder']
        fecha_limite = ahora + timedelta(days=dias_responder)
        
        # 4. Obtener instalaciones activas
        query_inst = f"""
        SELECT DISTINCT cliente_rol, instalacion_rol
        FROM `{TABLE_USUARIO_INST}`
        WHERE puede_ver = TRUE
        ORDER BY cliente_rol, instalacion_rol
        """
        instalaciones = list(client.query(query_inst).result())
        
        encuestas_creadas = []
        notificaciones_programadas = []
        
        # 5. Generar encuestas por instalación
        for inst in instalaciones:
            cliente_rol = inst.cliente_rol
            instalacion_rol = inst.instalacion_rol
            
            # 5.1 Crear encuesta COMPARTIDA
            encuesta_compartida_id = str(uuid.uuid4())
            encuesta_compartida = {
                'encuesta_id': encuesta_compartida_id,
                'periodo': periodo,
                'cliente_rol': cliente_rol,
                'instalacion_rol': instalacion_rol,
                'modo': 'compartida',
                'email_destinatario': None,
                'estado': 'pendiente',
                'fecha_creacion': ahora,
                'fecha_limite': fecha_limite,
                'respondido_por_email': None,
                'respondido_por_nombre': None,
                'tipo_respuesta': None,
                'fecha_respuesta': None
            }
            encuestas_creadas.append(encuesta_compartida)
            
            # 5.2 Usuarios con encuesta INDIVIDUAL
            query_individuales = f"""
            SELECT DISTINCT u.email_login
            FROM `{TABLE_USUARIO_INST}` ui
            INNER JOIN `{TABLE_USUARIOS}` u ON ui.email_login = u.email_login
            WHERE ui.cliente_rol = @cliente_rol
              AND ui.instalacion_rol = @instalacion_rol
              AND ui.requiere_encuesta_individual = TRUE
              AND ui.puede_ver = TRUE
              AND u.activo = TRUE
            """
            job_config_ind = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cliente_rol", "STRING", cliente_rol),
                    bigquery.ScalarQueryParameter("instalacion_rol", "STRING", instalacion_rol),
                ]
            )
            usuarios_individuales = list(client.query(query_individuales, job_config=job_config_ind).result())
            
            # 5.3 Crear encuestas individuales
            for usuario in usuarios_individuales:
                encuesta_individual = {
                    'encuesta_id': str(uuid.uuid4()),
                    'periodo': periodo,
                    'cliente_rol': cliente_rol,
                    'instalacion_rol': instalacion_rol,
                    'modo': 'individual',
                    'email_destinatario': usuario.email_login,
                    'estado': 'pendiente',
                    'fecha_creacion': ahora,
                    'fecha_limite': fecha_limite,
                    'respondido_por_email': None,
                    'respondido_por_nombre': None,
                    'tipo_respuesta': None,
                    'fecha_respuesta': None
                }
                encuestas_creadas.append(encuesta_individual)
            
            # 5.4 Usuarios con FCM token (para notificaciones)
            query_fcm = f"""
            SELECT 
                u.email_login,
                u.fcm_token,
                ui.requiere_encuesta_individual as requiere_individual
            FROM `{TABLE_USUARIO_INST}` ui
            INNER JOIN `{TABLE_USUARIOS}` u ON ui.email_login = u.email_login
            WHERE ui.cliente_rol = @cliente_rol
              AND ui.instalacion_rol = @instalacion_rol
              AND ui.puede_ver = TRUE
              AND u.activo = TRUE
              AND u.fcm_token IS NOT NULL
              AND u.fcm_token != ''
            """
            job_config_fcm = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cliente_rol", "STRING", cliente_rol),
                    bigquery.ScalarQueryParameter("instalacion_rol", "STRING", instalacion_rol),
                ]
            )
            usuarios_fcm = list(client.query(query_fcm, job_config=job_config_fcm).result())
            
            # 5.5 Programar notificaciones
            for usuario in usuarios_fcm:
                # Determinar encuesta
                encuesta_id = None
                if usuario.requiere_individual:
                    for enc in encuestas_creadas:
                        if (enc['modo'] == 'individual' and 
                            enc['email_destinatario'] == usuario.email_login):
                            encuesta_id = enc['encuesta_id']
                            break
                else:
                    encuesta_id = encuesta_compartida_id
                
                if encuesta_id:
                    notifs = programar_notificaciones(
                        encuesta_id=encuesta_id,
                        fcm_token=usuario.fcm_token,
                        instalacion=instalacion_rol,
                        fecha_inicio=ahora,
                        dia_rec1=config['dia_recordatorio_1'],
                        dia_rec2=config['dia_recordatorio_2'],
                        horario_inicio=config['horario_inicio'],
                        dias_laborales=config['dias_laborales']
                    )
                    notificaciones_programadas.extend(notifs)
        
        # 6. Insertar encuestas
        if encuestas_creadas:
            errors = client.insert_rows_json(TABLE_ENCUESTAS_SOLICITUDES, encuestas_creadas)
            if errors:
                raise HTTPException(status_code=500, detail=f"Error insertando encuestas: {errors}")
        
        # 7. Insertar notificaciones
        if notificaciones_programadas:
            errors = client.insert_rows_json(TABLE_ENCUESTAS_NOTIF_PROG, notificaciones_programadas)
            if errors:
                raise HTTPException(status_code=500, detail=f"Error insertando notificaciones: {errors}")
        
        return {
            "success": True,
            "periodo": periodo,
            "encuestas_creadas": len(encuestas_creadas),
            "notificaciones_programadas": len(notificaciones_programadas)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# ENDPOINT 2: ENVIAR NOTIFICACIONES
# ============================================

@app.post("/api/notificaciones/enviar")
async def enviar_notificaciones_push():
    """
    Envía notificaciones push programadas.
    Se ejecuta diariamente vía Cloud Scheduler.
    """
    try:
        # Obtener hora actual en zona horaria de Chile
        tz_chile = pytz.timezone('America/Santiago')
        ahora_utc = datetime.now(pytz.UTC)
        ahora_chile = ahora_utc.astimezone(tz_chile)
        
        print(f"🕐 Hora UTC: {ahora_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"🕐 Hora Chile: {ahora_chile.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        
        # 1. Verificar horario permitido
        query_config = f"""
        SELECT horario_inicio, horario_fin, dias_laborales, notificaciones_activas
        FROM `{TABLE_ENCUESTAS_CONFIG}`
        WHERE activo = TRUE
        LIMIT 1
        """
        config_result = list(client.query(query_config).result())
        if not config_result:
            raise HTTPException(status_code=400, detail="No hay configuración activa")
        
        config = dict(config_result[0])
        
        # Verificar si están activas
        if not config['notificaciones_activas']:
            print("⚠️ Notificaciones desactivadas")
            return {"success": True, "message": "Notificaciones desactivadas", "enviadas": 0}
        
        # Verificar día laboral (usar hora de Chile)
        dia_semana = ahora_chile.weekday()  # 0=Lunes, 1=Martes, ..., 4=Viernes, 5=Sábado, 6=Domingo
        if dia_semana not in config['dias_laborales']:
            print(f"⚠️ Fuera de días laborales. Día actual: {dia_semana}, Días permitidos: {config['dias_laborales']}")
            return {"success": True, "message": "Fuera de días laborales", "enviadas": 0}
        
        # Verificar horario (usar hora de Chile)
        hora_actual = ahora_chile.hour
        if hora_actual < config['horario_inicio'] or hora_actual >= config['horario_fin']:
            print(f"⚠️ Fuera de horario. Hora actual: {hora_actual}, Horario permitido: {config['horario_inicio']}-{config['horario_fin']}")
            return {"success": True, "message": "Fuera de horario", "enviadas": 0}
        
        # 2. Obtener notificaciones pendientes (comparar en UTC)
        query_notif = f"""
        SELECT *
        FROM `{TABLE_ENCUESTAS_NOTIF_PROG}`
        WHERE estado = 'pendiente'
          AND fecha_programada <= @ahora_utc
        ORDER BY fecha_programada ASC
        LIMIT 100
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("ahora_utc", "TIMESTAMP", ahora_utc),
            ]
        )
        print(f"📊 Buscando notificaciones programadas antes de: {ahora_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        notificaciones = list(client.query(query_notif, job_config=job_config).result())
        
        if not notificaciones:
            return {"success": True, "message": "No hay notificaciones pendientes", "enviadas": 0}
        
        # 3. Obtener access token
        access_token = obtener_fcm_access_token()
        
        # 4. Enviar notificaciones
        enviadas = 0
        fallidas = 0
        logs = []
        
        for notif in notificaciones:
            try:
                notif_dict = dict(notif)
                resultado = enviar_fcm(
                    fcm_token=notif_dict['fcm_token'],
                    titulo=notif_dict['titulo'],
                    cuerpo=notif_dict['cuerpo'],
                    data=json.loads(notif_dict['data']) if notif_dict['data'] else {},
                    access_token=access_token
                )
                
                if resultado['success']:
                    # Actualizar como enviada
                    query_update = f"""
                    UPDATE `{TABLE_ENCUESTAS_NOTIF_PROG}`
                    SET estado = 'enviada', fecha_envio = CURRENT_TIMESTAMP()
                    WHERE notificacion_id = @notif_id
                    """
                    job_config_upd = bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("notif_id", "STRING", notif_dict['notificacion_id']),
                        ]
                    )
                    client.query(query_update, job_config=job_config_upd).result()
                    enviadas += 1
                    
                    # Log exitoso
                    logs.append({
                        'log_id': str(uuid.uuid4()),
                        'encuesta_id': json.loads(notif_dict['data'])['encuesta_id'],
                        'email_destinatario': obtener_email_por_token(notif_dict['fcm_token']),
                        'tipo_notificacion': json.loads(notif_dict['data'])['tipo'],
                        'fecha_envio': ahora_utc.isoformat(),
                        'estado': 'exitoso',
                        'error_mensaje': None
                    })
                else:
                    # Actualizar como fallida
                    query_update_fail = f"""
                    UPDATE `{TABLE_ENCUESTAS_NOTIF_PROG}`
                    SET estado = 'fallida', error_mensaje = @error
                    WHERE notificacion_id = @notif_id
                    """
                    job_config_fail = bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("notif_id", "STRING", notif_dict['notificacion_id']),
                            bigquery.ScalarQueryParameter("error", "STRING", resultado['error']),
                        ]
                    )
                    client.query(query_update_fail, job_config=job_config_fail).result()
                    fallidas += 1
                    
                    # Log fallido
                    logs.append({
                        'log_id': str(uuid.uuid4()),
                        'encuesta_id': json.loads(notif_dict['data'])['encuesta_id'],
                        'email_destinatario': obtener_email_por_token(notif_dict['fcm_token']),
                        'tipo_notificacion': json.loads(notif_dict['data'])['tipo'],
                        'fecha_envio': ahora_utc.isoformat(),
                        'estado': 'fallido',
                        'error_mensaje': resultado['error']
                    })
                    
            except Exception as e:
                print(f"❌ Error procesando notificación: {e}")
                fallidas += 1
        
        # 5. Insertar logs
        if logs:
            errors = client.insert_rows_json(TABLE_ENCUESTAS_NOTIF_LOG, logs)
            if errors:
                print(f"⚠️ Error insertando logs: {errors}")
        
        return {
            "success": True,
            "enviadas": enviadas,
            "fallidas": fallidas,
            "total": len(notificaciones)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# FUNCIONES AUXILIARES
# ============================================

def programar_notificaciones(encuesta_id, fcm_token, instalacion, 
                             fecha_inicio, dia_rec1, dia_rec2, 
                             horario_inicio, dias_laborales):
    """Programa 3 notificaciones"""
    notificaciones = []
    
    # Notificación 1: Nueva encuesta
    fecha_1 = ajustar_fecha_laboral(fecha_inicio, horario_inicio, dias_laborales)
    notificaciones.append({
        'notificacion_id': str(uuid.uuid4()),
        'fcm_token': fcm_token,
        'titulo': '📋 Nueva Encuesta Disponible',
        'cuerpo': f'Tiene una nueva encuesta de satisfacción para {instalacion}',
        'data': json.dumps({"encuesta_id": encuesta_id, "tipo": "nueva"}),
        'fecha_programada': fecha_1.isoformat(),
        'estado': 'pendiente',
        'fecha_envio': None,
        'error_mensaje': None
    })
    
    # Notificación 2: Recordatorio 1
    fecha_2 = ajustar_fecha_laboral(
        fecha_inicio + timedelta(days=dia_rec1 - 1),
        horario_inicio,
        dias_laborales
    )
    notificaciones.append({
        'notificacion_id': str(uuid.uuid4()),
        'fcm_token': fcm_token,
        'titulo': '🔔 Recordatorio de Encuesta',
        'cuerpo': f'Recuerde completar la encuesta de {instalacion}',
        'data': json.dumps({"encuesta_id": encuesta_id, "tipo": "recordatorio_1"}),
        'fecha_programada': fecha_2.isoformat(),
        'estado': 'pendiente',
        'fecha_envio': None,
        'error_mensaje': None
    })
    
    # Notificación 3: Recordatorio 2
    fecha_3 = ajustar_fecha_laboral(
        fecha_inicio + timedelta(days=dia_rec2 - 1),
        horario_inicio,
        dias_laborales
    )
    notificaciones.append({
        'notificacion_id': str(uuid.uuid4()),
        'fcm_token': fcm_token,
        'titulo': '⚠️ Último Recordatorio',
        'cuerpo': f'La encuesta de {instalacion} vence pronto',
        'data': json.dumps({"encuesta_id": encuesta_id, "tipo": "recordatorio_2"}),
        'fecha_programada': fecha_3.isoformat(),
        'estado': 'pendiente',
        'fecha_envio': None,
        'error_mensaje': None
    })
    
    return notificaciones


def ajustar_fecha_laboral(fecha, hora_inicio, dias_laborales):
    """Ajusta fecha a día laboral"""
    # dias_laborales usa formato weekday(): 0=Lunes, 1=Martes, ..., 4=Viernes
    while fecha.weekday() not in dias_laborales:
        fecha += timedelta(days=1)
    fecha = fecha.replace(hour=hora_inicio, minute=0, second=0, microsecond=0)
    return fecha


def obtener_fcm_access_token():
    """Obtiene access token para FCM"""
    credentials, project = google.auth.default(
        scopes=['https://www.googleapis.com/auth/firebase.messaging']
    )
    request = google.auth.transport.requests.Request()
    credentials.refresh(request)
    return credentials.token


def enviar_fcm(fcm_token, titulo, cuerpo, data, access_token):
    """Envía notificación FCM"""
    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }
    payload = {
        'message': {
            'token': fcm_token,
            'notification': {'title': titulo, 'body': cuerpo},
            'data': data,
            'android': {'priority': 'high'}
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            return {'success': True, 'error': None}
        else:
            return {'success': False, 'error': f"{response.status_code} - {response.text}"}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def obtener_email_por_token(fcm_token):
    """Obtiene email por FCM token"""
    query = f"""
    SELECT email_login
    FROM `{TABLE_USUARIOS}`
    WHERE fcm_token = @token
    LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("token", "STRING", fcm_token),
        ]
    )
    result = list(client.query(query, job_config=job_config).result())
    return result[0].email_login if result else None

# ============================================
# EJECUTAR
# ============================================

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
