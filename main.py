import os
import uuid
import time
import threading
import logging
from datetime import datetime
from flask import Flask, request, abort
from telebot import TeleBot, types
from pymongo import MongoClient
import certifi
from pymongo.server_api import ServerApi
import telebot
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

load_dotenv()
app = Flask(__name__)

LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")
API_KEYS = []
for i in range(1, 7):
    bot_token = os.environ.get(f"BOT_TOKEN_{i}")
    force_sub_channel = os.environ.get(f"FORCE_SUB_CHANNEL_{i}", "0")
    private_group_id = os.environ.get(f"PRIVATE_GROUP_ID{i}")
    admins = os.environ.get(f"ADMINS{i}", "").split(",")
    mongo_uri = os.environ.get(f"mongo_uri_{i}")
    CALLURL = os.environ.get("CALLURL")
    OWNER_ID = os.environ.get("OWNER_ID")

    if bot_token and private_group_id and mongo_uri:
        API_KEYS.append((
            bot_token,
            force_sub_channel,
            int(private_group_id),
            [int(admin) for admin in admins if admin.strip().isdigit()],
            mongo_uri,
            OWNER_ID  
        ))
    else:
        logging.warning(f"Bot #{i} is missing required configuration (token, private_group_id, or mongo_uri).")

WAIT_MSG_HANDLE_FILES = "<b>⌛ Please Wait...</b>"

BOT_INSTANCES = {}

def create_bot(api_key):
    # Unpack configuration (OWNER_ID is passed as the last element)
    bot_token, force_channel, private_group_id, admin_ids, mongo_uri, OWNER_ID = api_key

    try:
        client = MongoClient(mongo_uri, server_api=ServerApi('1'), tlsCAFile=certifi.where())
        db = client['telegram_bot']
        users_collection = db['users']
        file_storage_collection = db['file_storage']
        logging.info(f"Connected to MongoDB for mongo_uri: {mongo_uri}")
    except Exception as e:
        logging.error(f"Failed to connect to MongoDB for URI {mongo_uri}: {e}")
        raise

    bot = TeleBot(bot_token)
    bot.remove_webhook()

    bot_info = bot.get_me()
    bot_username = bot_info.username
    logging.info(f"Creating bot @{bot_username}.")

    broadcast_context = {}

    if force_channel != "0":
        try:
            channel = bot.get_chat(force_channel)
            logging.info(f"Force channel {force_channel} found for bot @{bot_username}.")
        except Exception as e:
            logging.error(f"Invalid force channel ({force_channel}) for bot @{bot_username}. Disabling force subscription. Error: {e}")
            force_channel = "0"

    def save_user(chat_id):
        query = {"chat_id": str(chat_id), "bot_username": bot_username}
        if users_collection.find_one(query):
            logging.info(f"User {chat_id} already exists permanently for bot @{bot_username}.")
            return
        try:
            users_collection.insert_one({
                "chat_id": str(chat_id),
                "bot_username": bot_username,
                "joined_at": datetime.utcnow(),
                "subscribed": False
            })
            logging.info(f"Saved user {chat_id} permanently for bot @{bot_username}.")
        except Exception as e:
            logging.error(f"Failed to save user {chat_id} for bot @{bot_username}: {e}")

    def save_file_storage(unique_id, file_info):
        if file_storage_collection.find_one({'unique_id': unique_id}):
            logging.info(f"File {unique_id} already exists permanently in the database.")
            return
        try:
            file_storage_collection.insert_one({
                "unique_id": unique_id,
                "file_id": file_info[0],
                "file_type": file_info[1]
            })
            logging.info(f"File {unique_id} stored permanently in the database.")
        except Exception as e:
            logging.error(f"Failed to store file {unique_id}: {e}")

    def load_file_storage(unique_id):
        return file_storage_collection.find_one({'unique_id': unique_id})

    def send_file(chat_id, file_id, file_type):
        try:
            sent_message = None
            if file_type == 'photo':
                sent_message = bot.send_photo(chat_id, file_id, protect_content=True)
            elif file_type == 'video':
                sent_message = bot.send_video(chat_id, file_id, protect_content=True)
            elif file_type == 'document':
                sent_message = bot.send_document(chat_id, file_id, protect_content=True)
            elif file_type == 'audio':
                sent_message = bot.send_audio(chat_id, file_id, protect_content=True)
            elif file_type == 'voice':
                sent_message = bot.send_voice(chat_id, file_id, protect_content=True)
            if sent_message:
                threading.Thread(target=delete_message_after_delay, args=(chat_id, sent_message.message_id)).start()
        except Exception as e:
            logging.error(f"Error sending file to chat {chat_id} for bot @{bot_username}: {e}")

    def delete_message_after_delay(chat_id, message_id):
        time.sleep(1200)  # 20 minutes
        try:
            bot.delete_message(chat_id, message_id)
            logging.info(f"Deleted message {message_id} from chat {chat_id} for bot @{bot_username}.")
        except Exception as e:
            logging.error(f"Failed to delete message {message_id} for bot @{bot_username}: {e}")

    @bot.message_handler(commands=['start'])
    def handle_start(message):
        save_user(message.chat.id)
        # Only check force subscription for individual users (chat id > 0)
        # and if the user is not the owner.
        if force_channel != "0" and message.chat.id > 0 and (OWNER_ID is None or message.chat.id != OWNER_ID):
            try:
                member_status = bot.get_chat_member(force_channel, message.chat.id)
                if member_status.status not in ['member', 'administrator']:
                    raise Exception("Not a member")
            except Exception as e:
                send_force_subscribe_message(message.chat.id)
                return
        # Process /start command arguments if any.
        args = message.text.split()
        if len(args) > 1:
            unique_id = args[1]
            send_file_by_id(message, unique_id)
        else:
            send_welcome_message(message)

    def send_welcome_message(message):
        user_name = message.from_user.first_name or message.from_user.username
        greeting_text = f"Hello, *{user_name}*! 😉\n\nWelcome to our bot. Enjoy your stay!"
        markup = types.InlineKeyboardMarkup(row_width=2)
        channel_button = types.InlineKeyboardButton("Chat Channel", url="https://t.me/+tvWHQ58slElmNmQ1")
        close_button = types.InlineKeyboardButton("Close", callback_data="close")
        markup.add(channel_button, close_button)
        bot.send_message(message.chat.id, greeting_text, parse_mode="Markdown", reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data == "close")
    def close_button(call):
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.error(f"Failed to delete message with buttons for bot @{bot_username}: {e}")

    @bot.message_handler(func=lambda message: message.chat.id == private_group_id and message.from_user.id in admin_ids,
                         content_types=['photo', 'video', 'document', 'audio', 'voice'])
    def handle_files(message):
        try:
            file_info = None
            if message.photo:
                file_info = (message.photo[-1].file_id, 'photo')
            elif message.video:
                file_info = (message.video.file_id, 'video')
            elif message.document:
                file_info = (message.document.file_id, 'document')
            elif message.audio:
                file_info = (message.audio.file_id, 'audio')
            elif message.voice:
                file_info = (message.voice.file_id, 'voice')
            if file_info:
                unique_id = str(uuid.uuid4())
                while load_file_storage(unique_id):
                    unique_id = str(uuid.uuid4())
                save_file_storage(unique_id, file_info)
                shareable_link = f"https://t.me/{bot_username}?start={unique_id}"
                processing_msg = bot.send_message(message.chat.id, WAIT_MSG_HANDLE_FILES)
                bot.edit_message_text(
                    f"File stored! Use this link to access it: {shareable_link}",
                    message.chat.id,
                    processing_msg.message_id
                )
                logging.info(f"Stored file for bot @{bot_username} with unique id {unique_id}.")
        except Exception as e:
            logging.error(f"Error processing file for bot @{bot_username}: {e}")
            bot.reply_to(message, "An error occurred while processing the file.")

    def send_file_by_id(message, unique_id):
        file_info = load_file_storage(unique_id)
        if file_info:
            send_file(message.chat.id, file_info['file_id'], file_info['file_type'])
        else:
            bot.send_message(message.chat.id, "File not found!")
            logging.info(f"File with unique id {unique_id} not found for chat {message.chat.id} on bot @{bot_username}.")

    @bot.message_handler(commands=['help'])
    def handle_help(message):
        bot.send_message(message.chat.id, "<b>Use /start to interact with the bot!</b>", parse_mode="HTML")

    def send_force_subscribe_message(chat_id):
        # Skip force subscription for groups/channels.
        if chat_id < 0:
            logging.info(f"Chat {chat_id} is a group/channel. Skipping force subscribe message.")
            return
        # Skip force subscription for owner.
        if OWNER_ID and chat_id == OWNER_ID:
            logging.info(f"Chat {chat_id} is the owner. Skipping force subscribe message.")
            return
        if force_channel == "0":
            return
        try:
            channel = bot.get_chat(force_channel)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Join Channel", url=f"https://t.me/{channel.username}"))
            bot.send_message(
                chat_id,
                "*You need to join our compulsory channel😇 \n\nClick the link below to join 🔗 :*",
                reply_markup=markup,
                parse_mode="Markdown"
            )
            logging.info(f"Sent force subscribe message to user {chat_id} for bot @{bot_username}.")
        except Exception as e:
            logging.error(f"Failed to send subscription message to user {chat_id} for bot @{bot_username}: {e}")

    # --- Broadcast Functionality for Owner Only ---
    def broadcast_message(context, owner_id):
        """
        Sends the broadcast message to all users.
        If an image is attached, sends a single photo with caption;
        otherwise, sends a text message.
        After completion, sends a summary report to the owner and updates the prompt message to "Broadcasting Completed".
        """
        text = context['text']
        image_file_id = context.get('image_file_id')
        total_count = 0
        sent_count = 0
        blocked_count = 0
        try:
            users = users_collection.find({"bot_username": bot_username})
            for user in users:
                total_count += 1
                try:
                    chat_id = int(user['chat_id'])
                    if image_file_id:
                        bot.send_photo(chat_id, image_file_id, caption=text, protect_content=True)
                    else:
                        bot.send_message(chat_id, text, protect_content=True)
                    sent_count += 1
                except Exception as e:
                    logging.error(f"Broadcast error for chat {chat_id}: {e}")
                    blocked_count += 1
            summary = (
                f"<b>Broadcast Completed</b>\n\n"
                f"<b>Total Users:</b> {total_count}\n"
                f"<b>Sent:</b> {sent_count}\n"
                f"<b>Blocked:</b> {blocked_count}"
            )
            bot.send_message(owner_id, summary, parse_mode="HTML")
            # Update the original prompt message to indicate completion.
            if 'prompt_chat_id' in context and 'prompt_message_id' in context:
                try:
                    bot.edit_message_text("Broadcasting Completed", context['prompt_chat_id'], context['prompt_message_id'])
                except Exception as e:
                    logging.error(f"Error editing prompt message: {e}")
        except Exception as e:
            logging.error(f"Error during broadcast: {e}")
        finally:
            if owner_id in broadcast_context:
                del broadcast_context[owner_id]

    @bot.message_handler(commands=['sendall', 'senall'])
    def handle_sendall(message):
        # Only allow command if issued in a private chat by the owner.
        if message.chat.type != "private" or message.from_user.id != OWNER_ID:
            return
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "Usage: /sendall @YourBotUsername Your message here")
            return
        target_username = parts[1]
        if target_username != f"@{bot_username}":
            bot.reply_to(message, "Incorrect bot username in command.")
            return
        broadcast_text = parts[2]
        broadcast_context[message.from_user.id] = {
            'text': broadcast_text,
            'with_image': False,
            'image_file_id': None,
            'state': 'pending',
            'prompt_message_id': None,
            'prompt_chat_id': message.chat.id
        }
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("Yes", callback_data="broadcast_yes"),
            types.InlineKeyboardButton("No", callback_data="broadcast_no")
        )
        prompt_msg = bot.send_message(message.chat.id, "Do you want to attach an image?", reply_markup=markup)
        broadcast_context[message.from_user.id]['prompt_message_id'] = prompt_msg.message_id

    @bot.callback_query_handler(func=lambda call: call.data in ["broadcast_yes", "broadcast_no", "broadcast_cancel"])
    def handle_broadcast_choice(call):
        owner_id = call.from_user.id
        if owner_id not in broadcast_context:
            bot.answer_callback_query(call.id, "No broadcast pending.")
            return
        context = broadcast_context[owner_id]
        if call.data == "broadcast_no":
            try:
                bot.edit_message_text("Broadcasting...", call.message.chat.id, call.message.message_id)
            except Exception as e:
                logging.error(f"Error editing message: {e}")
            context['state'] = 'broadcasting'
            threading.Thread(target=broadcast_message, args=(context, owner_id)).start()
            bot.answer_callback_query(call.id, "Broadcast started.")
        elif call.data == "broadcast_yes":
            # Update prompt to ask for image upload with a Cancel button.
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Cancel", callback_data="broadcast_cancel"))
            try:
                bot.edit_message_text("Please upload an image to attach to the broadcast.", call.message.chat.id, call.message.message_id, reply_markup=markup)
            except Exception as e:
                logging.error(f"Error editing message: {e}")
            context['state'] = 'awaiting_image'
            bot.answer_callback_query(call.id, "Awaiting image upload.")
        elif call.data == "broadcast_cancel":
            try:
                bot.edit_message_text("Command canceled", call.message.chat.id, call.message.message_id)
            except Exception as e:
                logging.error(f"Error editing message on cancel: {e}")
            del broadcast_context[owner_id]
            bot.answer_callback_query(call.id, "Broadcast canceled.")

    @bot.message_handler(func=lambda message: message.from_user.id in broadcast_context and 
                         broadcast_context[message.from_user.id].get('state') == 'awaiting_image',
                         content_types=['photo'])
    def handle_broadcast_image(message):
        owner_id = message.from_user.id
        context = broadcast_context[owner_id]
        if message.photo:
            image_file_id = message.photo[-1].file_id
            context['image_file_id'] = image_file_id
            context['with_image'] = True
            context['state'] = 'broadcasting'
            try:
                bot.edit_message_text("Image received. Broadcasting...", context['prompt_chat_id'], context['prompt_message_id'])
            except Exception as e:
                logging.error(f"Error editing message after image reception: {e}")
            threading.Thread(target=broadcast_message, args=(context, owner_id)).start()

    # --- New: Forward all messages to the log channel ---
    @bot.message_handler(func=lambda message: True, content_types=[
        'text', 'photo', 'audio', 'video', 'document',
        'sticker', 'voice', 'location', 'contact', 'video_note'
    ])
    def forward_to_log_channel(message):
        if LOG_CHANNEL_ID is None:
            # If no log channel is configured, skip forwarding.
            return
        try:
            bot.forward_message(LOG_CHANNEL_ID, message.chat.id, message.message_id)
        except Exception as e:
            logging.error(f"Failed to forward message from chat {message.chat.id}: {e}")

    # --- End of Forwarding Handler ---

    try:
        webhook_url = f"{CALLURL}/webhook/{bot_username}"
        bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook set for bot @{bot_username} at {webhook_url}")
    except Exception as e:
        logging.error(f"Error setting webhook for bot @{bot_username}: {e}")

    return bot

for api_key in API_KEYS:
    try:
        bot_instance = create_bot(api_key)
        bot_username = bot_instance.get_me().username
        BOT_INSTANCES[bot_username] = bot_instance
        logging.info(f"Bot instance created for @{bot_username}.")
    except Exception as e:
        logging.error(f"Error creating bot instance: {e}")

# --- Webhook Handler ---
@app.route('/webhook/<bot_username>', methods=['POST'])
def webhook(bot_username):
    if bot_username not in BOT_INSTANCES:
        logging.error(f"Webhook request received for unknown bot: {bot_username}")
        abort(404)
    bot = BOT_INSTANCES[bot_username]
    json_str = request.get_data().decode('UTF-8')
    try:
        update = telebot.types.Update.de_json(json_str)
        threading.Thread(target=bot.process_new_updates, args=([update],)).start()
        logging.info(f"Spawned thread to process update for bot @{bot_username}.")
    except Exception as e:
        logging.error(f"Error processing update for bot @{bot_username}: {e}")
    return "OK", 200

@app.route('/', methods=['GET'])
def home():
    return "Hello, this is the home page for your Flask app!"

@app.route('/', methods=['POST'])
def handle_post():
    return "POST request received!"

if __name__ == "__main__":
    logging.info("Starting Flask app...")
    app.run(host='0.0.0.0', port=5000, threaded=True)
