import os
import logging
import json
from datetime import datetime, timedelta, time
import re

import pandas as pd
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# --- Configuración Inicial ---

# Cargar las variables de entorno desde el archivo .env
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configurar el logging para registrar eventos del bot.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Configurar el modelo de Gemini.
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash-lite")

# Cargar el archivo de Excel y preparar el DataFrame.
try:
    df = pd.read_excel("Horarios.xlsx", sheet_name="Horarios", header=0)
    df.columns = df.columns.str.strip()
    df['Salida'] = pd.to_datetime(df['Salida'], format='%H:%M:%S').dt.time
    df['Llegada'] = pd.to_datetime(df['Llegada'], format='%H:%M:%S').dt.time
except FileNotFoundError:
    logging.error("No se encontró el archivo de Excel 'Horarios.xlsx'.")
    df = pd.DataFrame()


# --- Funciones Auxiliares ---

def buscar_horarios(df, intent):
    """
    Filtra el DataFrame según la intención del usuario.
    
    Args:
        df (pd.DataFrame): El DataFrame con todos los horarios.
        intent (dict): Un diccionario con la intención del usuario.
        
    Returns:
        pd.DataFrame: Un DataFrame con los horarios filtrados.
    """
    if df.empty:
        return pd.DataFrame()

    # Extraer parámetros de la intención del usuario.
    direccion = intent.get('direccion')
    hora_str = intent.get('hora')
    accion = intent.get('accion')
    condicion_horario = intent.get('condicion_horario')
    cantidad = intent.get('cantidad')
    listado_completo = intent.get('listado_completo', False)
    micro_linea = intent.get('micro_linea')

    # Corrección: Manejar el caso en que la IA devuelve una lista en lugar de una cadena.
    if isinstance(direccion, list) and len(direccion) > 0:
        direccion = direccion[0]
    if isinstance(micro_linea, list) and len(micro_linea) > 0:
        micro_linea = micro_linea[0]
    
    # Si se pide una línea de micro específica, el resto de la lógica no aplica de la misma forma.
    if micro_linea:
        df_filtrado = df[df['Línea'].str.lower().str.contains(micro_linea.lower(), na=False)].copy()
        
        # Opcionalmente, puedes aplicar un filtro de dirección si también se especifica.
        if direccion:
            df_filtrado = df_filtrado[df_filtrado['Dirección'].str.lower() == direccion.lower()].copy()
            
        return df_filtrado.sort_values(by='Salida')

    if not direccion:
        return pd.DataFrame()
    
    df_filtrado = df[df['Dirección'].str.lower() == direccion.lower()].copy()

    # El filtro más importante: excluir todos los micros que ya salieron.
    df_filtrado['salida_datetime'] = df_filtrado['Salida'].apply(lambda t: datetime.combine(datetime.today(), t))
    df_filtrado['llegada_datetime'] = df_filtrado['Llegada'].apply(lambda t: datetime.combine(datetime.today(), t))
    df_filtrado = df_filtrado[df_filtrado['salida_datetime'] > datetime.now()]

    # Lógica para manejar la intención de "ahora".
    if hora_str == 'ahora':
        # Ordenar por el horario de salida más cercano.
        proximos = df_filtrado.sort_values(by='salida_datetime')
        if listado_completo:
            return proximos
        elif cantidad is not None:
            return proximos.head(cantidad)
        else:
            return proximos.head(3)

    # Lógica para manejar la intención con una hora específica.
    elif hora_str:
        try:
            hora_solicitada_dt = datetime.combine(datetime.today(), datetime.strptime(hora_str, '%H:%M').time())
            
            # Condición para "cerca de"
            if condicion_horario == 'cerca':
                # Micros que llegan antes o en la hora solicitada.
                micros_antes = df_filtrado[df_filtrado['llegada_datetime'] <= hora_solicitada_dt].copy()
                # Micros que llegan después de la hora solicitada.
                micros_despues = df_filtrado[df_filtrado['llegada_datetime'] > hora_solicitada_dt].copy()

                resultados = pd.DataFrame()

                # Obtener el micro más cercano antes/en la hora solicitada.
                if not micros_antes.empty:
                    cercano_antes = micros_antes.sort_values(by='llegada_datetime', ascending=False).head(1)
                    resultados = pd.concat([resultados, cercano_antes])

                # Obtener el micro más cercano después de la hora solicitada.
                if not micros_despues.empty:
                    cercano_despues = micros_despues.sort_values(by='llegada_datetime', ascending=True).head(1)
                    resultados = pd.concat([resultados, cercano_despues])
                
                return resultados.sort_values(by='llegada_datetime')

            # Condición para "llegar antes de".
            elif condicion_horario == 'antes_de':
                # Micros que llegan antes de la hora solicitada, ordenados por proximidad a esa hora.
                cercanos = df_filtrado[df_filtrado['llegada_datetime'] < hora_solicitada_dt].copy()
                cercanos['diff'] = (cercanos['llegada_datetime'] - hora_solicitada_dt).abs()
                cercanos = cercanos.sort_values(by='diff', ascending=True).drop('diff', axis=1)

                if listado_completo:
                    return cercanos
                elif cantidad is not None:
                    return cercanos.head(cantidad)
                else:
                    return cercanos # Devuelve todos si no se especifica cantidad.

            # Condición para "llegar después de".
            elif condicion_horario == 'despues_de':
                # Micros que llegan después de la hora solicitada, ordenados por proximidad a esa hora.
                cercanos = df_filtrado[df_filtrado['llegada_datetime'] >= hora_solicitada_dt].copy()
                cercanos['diff'] = (cercanos['llegada_datetime'] - hora_solicitada_dt).abs()
                cercanos = cercanos.sort_values(by='diff', ascending=True).drop('diff', axis=1)
                
                if listado_completo:
                    return cercanos
                elif cantidad is not None:
                    return cercanos.head(cantidad)
                else:
                    return cercanos # Devuelve todos si no se especifica cantidad.

        except ValueError:
            return pd.DataFrame()
    
    return pd.DataFrame()


# --- Manejadores de Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envía un mensaje cuando se ejecuta el comando /start."""
    await update.message.reply_text("¡Hola! Soy un bot que te ayuda a encontrar horarios de micros. Puedes preguntar cosas como: 'Quiero llegar a la facultad a las 15:00', 'Dame el listado completo de micros antes de las 19:00', o 'Dame los horarios del micro ruta 60'.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa el mensaje del usuario y responde con horarios de micros."""
    user_message = update.message.text
    
    if df.empty:
        await update.message.reply_text("Lo siento, no pude cargar la tabla de horarios. Por favor, revisa el archivo de Excel.")
        return

    # 1. Analizar el mensaje del usuario con Gemini.
    # Se crea una nueva sesión de chat para cada mensaje para evitar que el historial de conversaciones contamine nuevas solicitudes.
    chat = model.start_chat(history=[])
    prompt = (
        f"Analiza la siguiente solicitud para encontrar un micro. "
        f"Si el usuario quiere ir 'a la facultad', la 'Dirección' es 'Ida'. "
        f"Si quiere ir 'a Rivadavia', la 'Dirección' es 'Vuelta'. "
        f"Si quiere 'irse de la facultad', la 'Dirección' es 'Vuelta'. "
        f"Extrae la 'Hora' (ej. '15:30'). Si dice 'ya', la hora es 'ahora'. "
        f"Si quiere llegar a una hora, la 'acción' es 'llegar'; si quiere salir, la 'acción' es 'salir'. "
        f"Si menciona una línea de micro (ej. 'ruta 60'), extráela y ponla en 'micro_linea'. "
        f"Determina la 'condicion_horario' para la hora: 'cerca' (si dice 'alrededor de', 'a las'), 'antes_de' (si dice 'antes de') o 'despues_de' (si dice 'después de'). Si no se especifica, se puede dejar nulo. "
        f"Si pide una cantidad específica, extráela y ponla en 'cantidad'. "
        f"Si pide 'un listado de todos', establece 'listado_completo' en true. "
        f"Devuelve el resultado en formato JSON: "
        f"{{'direccion': 'Ida' o 'Vuelta' o null, 'hora': 'HH:MM' o 'ahora' o null, 'accion': 'llegar' o 'salir' o null, 'micro_linea': [nombre de la línea] o null, 'condicion_horario': 'cerca' o 'antes_de' o 'despues_de' o null, 'cantidad': [número] o null, 'listado_completo': [true o false]}}"
        f"Si la solicitud no es clara, devuelve el siguiente JSON: {{'error': 'no_claro'}}."
        f"\n\nSolicitud: '{user_message}'"
    )
    
    try:
        response = chat.send_message(prompt)
        
        if not response or not response.text:
            logging.error("La respuesta de Gemini está vacía o es nula.")
            await update.message.reply_text("Lo siento, hubo un problema al comunicarme con la IA. Por favor, intenta de nuevo.")
            return

        json_match = re.search(r'\{.*?\}', response.text, re.DOTALL)
        if not json_match:
            raise json.JSONDecodeError("No se encontró un objeto JSON válido.", response.text, 0)
        
        response_text = json_match.group(0).replace('\xa0', ' ')
        intent = json.loads(response_text)
        
        if 'error' in intent or (not intent.get('direccion') and not intent.get('micro_linea')):
            await update.message.reply_text("Lo siento, no entendí tu solicitud. Por favor, sé más específico.")
            return

        # 2. Consultar el DataFrame según la intención.
        horarios_disponibles = buscar_horarios(df, intent)
        
        # 3. Formatear y enviar la respuesta.
        if horarios_disponibles.empty:
            await update.message.reply_text("No se encontraron micros que se ajusten a tu búsqueda. Intenta con otra hora o modifica tu solicitud.")
        else:
            # Construir el encabezado dinámico para la respuesta.
            direccion_viaje = intent.get('direccion')
            header_text = ""
            if direccion_viaje and direccion_viaje.lower() == 'ida':
                header_text = "Acá están los horarios que encontré: Rivadavia -> Facultad\n\n"
            elif direccion_viaje and direccion_viaje.lower() == 'vuelta':
                header_text = "Acá están los horarios que encontré: Facultad -> Rivadavia\n\n"
            else:
                header_text = "Aquí están los horarios que encontré:\n\n"

            respuesta_bot = header_text
            for _, row in horarios_disponibles.iterrows():
                salida_str = row['Salida'].strftime('%H:%M') if isinstance(row['Salida'], time) else str(row['Salida'])
                llegada_str = row['Llegada'].strftime('%H:%M') if isinstance(row['Llegada'], time) else str(row['Llegada'])
                respuesta_bot += f"🚍 Línea **{row['Línea']}**\n"
                respuesta_bot += f"   ➡️ Sale a las `{salida_str}`\n"
                respuesta_bot += f"   ➡️ Llega a las `{llegada_str}`\n\n"
            await update.message.reply_text(respuesta_bot, parse_mode='Markdown')

    except json.JSONDecodeError as e:
        logging.error(f"Error al decodificar la respuesta JSON: {e}, Texto: '{response.text}'")
        await update.message.reply_text("Lo siento, la IA no devolvió una respuesta válida. Por favor, intenta con otra frase.")
    except Exception as e:
        logging.error(f"Error en el procesamiento del mensaje: {e}")
        await update.message.reply_text("Lo siento, hubo un error al procesar tu solicitud. Por favor, intenta de nuevo más tarde.")

# --- Función Principal ---

def main() -> None:
    """Inicia el bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
