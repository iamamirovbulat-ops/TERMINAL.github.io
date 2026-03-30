#!/usr/bin/env python3
import asyncio
import websockets
import json
import uuid
import qrcode
import io
import base64
import aiohttp
from aiohttp import web
from datetime import datetime
from pathlib import Path

# Конфигурация
import socket

# Получаем локальный IP адрес
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"

LOCAL_IP = get_local_ip()

BITRIX_CONFIG = {
    'webhook': 'https://sbu.bitrix24.ru/rest/1/r76d49oc0j2vktuz/',
    'pipeline_id': 24
}

PHONE_NUMBER = '89030619769'
HOST = '0.0.0.0'  # Слушаем на всех интерфейсах
PORT = 8765
HTTP_PORT = 8000

# Хранилище
active_sessions = {}
operators = {}

async def create_bitrix_deal(session_id):
    """Создание сделки в Битрикс24"""
    try:
        print(f"Creating Bitrix deal for session: {session_id}")
        print(f"Using pipeline ID: {BITRIX_CONFIG['pipeline_id']}")
        print(f"Webhook URL: {BITRIX_CONFIG['webhook']}crm.deal.add")
        
        deal_data = {
            'fields': {
                'TITLE': f'Обращение по терминалу #{session_id}',
                'STAGE_ID': 'NEW',
                'CATEGORY_ID': BITRIX_CONFIG['pipeline_id'],
                'SOURCE_ID': 'SELF',
                'ASSIGNED_BY_ID': 1,
                'BEGINDATE': datetime.now().strftime('%Y-%m-%d'),
                'COMMENTS': f'Сессия терминала: {session_id}\nВремя начала: {datetime.now().strftime("%d.%m.%Y %H:%M:%S")}\nТелефон: {PHONE_NUMBER}'
            }
        }
        
        print(f"Deal data: {json.dumps(deal_data, indent=2, ensure_ascii=False)}")
        
        async with aiohttp.ClientSession() as session:
            url = f"{BITRIX_CONFIG['webhook']}crm.deal.add"
            print(f"Sending POST request to: {url}")
            
            async with session.post(url, json=deal_data) as response:
                status = response.status
                print(f"Response status: {status}")
                
                result = await response.json()
                print(f"Bitrix response: {json.dumps(result, indent=2, ensure_ascii=False)}")
                
                if result.get('error'):
                    print(f"❌ Bitrix error: {result.get('error')}")
                    print(f"Error description: {result.get('error_description')}")
                    return None
                
                deal_id = result.get('result')
                if deal_id:
                    print(f"✅ Bitrix deal created successfully! ID: {deal_id}")
                else:
                    print(f"⚠️  No deal ID in response")
                    
                return deal_id
                
    except Exception as error:
        print(f"❌ Exception creating Bitrix deal: {error}")
        import traceback
        traceback.print_exc()
        return None

async def generate_qr_code(session_id, host):
    """Генерация QR-кода"""
    session_url = f"http://{LOCAL_IP}:{HTTP_PORT}/terminal/{session_id}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(session_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    img_str = base64.b64encode(buffer.getvalue()).decode()
    
    return f"data:image/png;base64,{img_str}"

async def handle_http(request):
    """Обработка HTTP запросов"""
    path = request.path_qs
    
    if path == '/api/generate-qr':
        session_id = str(uuid.uuid4())
        qr_code = await generate_qr_code(session_id, f"{LOCAL_IP}:{HTTP_PORT}")
        
        active_sessions[session_id] = {
            'id': session_id,
            'status': 'waiting',
            'created_at': datetime.now(),
            'terminal_id': 'unknown'
        }
        
        return web.json_response({
            'sessionId': session_id,
            'qrCode': qr_code,
            'phoneNumber': PHONE_NUMBER
        })
    
    # Статические файлы
    if path == '/':
        return web.FileResponse('public/terminal.html')
    elif path == '/operator':
        return web.FileResponse('public/operator.html')
    elif path.startswith('/terminal/'):
        # Извлекаем session_id из URL
        session_id = path.split('/')[2]
        
        # Создаем сессию если её нет
        if session_id not in active_sessions:
            active_sessions[session_id] = {
                'id': session_id,
                'status': 'waiting',
                'created_at': datetime.now(),
                'terminal_id': 'unknown'
            }
            # Создаем сделку в Битрикс при открытии страницы
            asyncio.create_task(create_bitrix_deal(session_id))
            print(f"New session created via URL: {session_id}")
        
        return web.FileResponse('public/terminal-session.html')
    
    return web.Response(text="Not Found", status=404)

async def handle_websocket(websocket):
    """Обработка WebSocket"""
    print(f"New connection: {websocket.remote_address}")
    
    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get('type')
            
            if msg_type == 'terminal-connect':
                session_id = data.get('sessionId')
                if session_id in active_sessions:
                    active_sessions[session_id]['terminal_websocket'] = websocket
                    active_sessions[session_id]['status'] = 'active'
                    
                    # Создаем сделку в Битрикс
                    asyncio.create_task(create_bitrix_deal(session_id))
                    
                    # Уведомляем операторов
                    for op_ws in list(operators.keys()):
                        try:
                            await op_ws.send(json.dumps({
                                'type': 'new-session',
                                'sessionId': session_id,
                                'message': 'Новый клиент ожидает консультации',
                                'timestamp': datetime.now().isoformat()
                            }))
                        except:
                            pass
                    
                    await websocket.send(json.dumps({
                        'type': 'session-started',
                        'sessionId': session_id
                    }))
            
            elif msg_type == 'operator-connect':
                operators[websocket] = {
                    'id': data.get('operatorId'),
                    'name': data.get('name', 'Оператор'),
                    'websocket': websocket
                }
                await websocket.send(json.dumps({'type': 'operator-connected'}))
            
            elif msg_type == 'accept-session':
                session_id = data.get('sessionId')
                operator = operators.get(websocket)
                
                if session_id in active_sessions and operator:
                    session = active_sessions[session_id]
                    session['operator_websocket'] = websocket
                    session['operator'] = operator
                    session['status'] = 'connected'
                    
                    terminal_ws = session.get('terminal_websocket')
                    if terminal_ws:
                        await terminal_ws.send(json.dumps({
                            'type': 'operator-connected',
                            'operatorName': operator['name']
                        }))
            
            elif msg_type in ['webrtc-offer', 'webrtc-answer', 'webrtc-ice-candidate']:
                # Передаем WebRTC сигналы между клиентом и оператором
                session_id = data.get('sessionId')
                if session_id in active_sessions:
                    session = active_sessions[session_id]
                    
                    # Определяем получателя
                    if websocket == session.get('terminal_websocket'):
                        target = session.get('operator_websocket')
                    else:
                        target = session.get('terminal_websocket')
                    
                    if target:
                        signal_data = {'type': msg_type, 'sessionId': session_id}
                        
                        if msg_type == 'webrtc-offer':
                            signal_data['offer'] = data.get('offer')
                        elif msg_type == 'webrtc-answer':
                            signal_data['answer'] = data.get('answer')
                        elif msg_type == 'webrtc-ice-candidate':
                            signal_data['candidate'] = data.get('candidate')
                        
                        await target.send(json.dumps(signal_data))
            
            elif msg_type == 'end-session':
                session_id = data.get('sessionId')
                if session_id in active_sessions:
                    session = active_sessions[session_id]
                    
                    # Уведомляем другую сторону
                    if websocket == session.get('terminal_websocket'):
                        target = session.get('operator_websocket')
                    else:
                        target = session.get('terminal_websocket')
                    
                    if target:
                        await target.send(json.dumps({'type': 'session-ended'}))
                    
                    del active_sessions[session_id]
                    
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # Чистка при отключении
        if websocket in operators:
            del operators[websocket]
        
        for sid, session in list(active_sessions.items()):
            if (session.get('terminal_websocket') == websocket or 
                session.get('operator_websocket') == websocket):
                
                other = (session.get('operator_websocket') 
                        if session.get('terminal_websocket') == websocket 
                        else session.get('terminal_websocket'))
                
                if other:
                    await other.send(json.dumps({'type': 'session-ended'}))
                
                del active_sessions[sid]
                break

async def main():
    """Запуск серверов"""
    # HTTP сервер
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handle_http)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, HTTP_PORT)
    await site.start()
    
    print(f"📡 HTTP сервер: http://{LOCAL_IP}:{HTTP_PORT}")
    print(f"📍 Терминал: http://{LOCAL_IP}:{HTTP_PORT}/")
    print(f"👨‍💼 Оператор: http://{LOCAL_IP}:{HTTP_PORT}/operator")
    
    # WebSocket сервер
    ws_server = await websockets.serve(handle_websocket, HOST, PORT)
    print(f"🔌 WebSocket: ws://{LOCAL_IP}:{PORT}")
    print("=" * 50)
    print("🚀 Система запущена! QR-код работает в локальной сети")
    print(f"📱 Сканируйте QR-код с устройств в той же сети")
    print("Остановить: Ctrl+C")
    
    await asyncio.Future()  # Вечный цикл

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Сервер остановлен")
