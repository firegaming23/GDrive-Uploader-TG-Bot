import re
import time
import json
import asyncio
from datetime import datetime, timedelta
from httplib2 import Http
from pyrogram import Client
from pyrogram.filters import private, incoming, command
from oauth2client.client import OAuth2Credentials
from helpers import gDrive_sql as db
from helpers import parent_id_sql as sql

OAUTH_SCOPE = "https://www.googleapis.com/auth/drive"
G_DRIVE_CLIENT_ID = "751038558683-qjamst56ahmeh68bbcr64tstpbkitvgd.apps.googleusercontent.com"
G_DRIVE_CLIENT_SECRET = "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"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEVICE_CODE_URI = "https://oauth2.googleapis.com/device/code"

user_auth_state = {}


@Client.on_message(private & incoming & command(['auth']))
async def _auth(client, message):
    creds = db.get_credential(message.from_user.id)
    if creds is not None:
        try:
            creds.refresh(Http())
            db.set_credential(message.from_user.id, creds)
            await message.reply_text(
                "🔒 **Already authorized your Google Drive Account.**\n"
                "__Use /revoke to revoke the current account.__\n"
                "__Send me a direct link or File to Upload on Google Drive__",
                quote=True
            )
        except Exception as e:
            await message.reply_text(f"**ERROR (refresh):** ```{e}```", quote=True)
        return

    try:
        http = Http()
        body = f"client_id={G_DRIVE_CLIENT_ID}&scope={OAUTH_SCOPE}"
        resp, content = http.request(
            DEVICE_CODE_URI,
            method="POST",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        data = json.loads(content)

        if resp.status != 200:
            await message.reply_text(f"**ERROR:** ```{data.get('error_description', content)}```", quote=True)
            return

        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_url = data["verification_url"]
        expires_in = data["expires_in"]
        interval = data["interval"]

        user_auth_state[message.from_user.id] = {
            "device_code": device_code,
            "interval": interval,
            "expires_at": time.time() + expires_in
        }

        await client.send_message(
            message.from_user.id,
            f"⛓️ **Authorize your Google Drive account.**\n\n"
            f"1. Visit: [{verification_url}]({verification_url})\n"
            f"2. Enter this code: `{user_code}`\n"
            f"3. Allow permissions\n\n"
            f"__I'll automatically detect when you're done. No need to send anything back.__"
        )

        asyncio.create_task(poll_for_token(client, message.from_user.id))

    except Exception as e:
        await message.reply_text(f"**ERROR:** ```{e}```", quote=True)


async def poll_for_token(client, user_id):
    state = user_auth_state.get(user_id)
    if not state:
        return

    device_code = state["device_code"]
    interval = state["interval"]
    expires_at = state["expires_at"]

    while time.time() < expires_at:
        await asyncio.sleep(interval)

        try:
            resp, content = await asyncio.get_event_loop().run_in_executor(
                None, _do_token_poll, device_code
            )
        except Exception:
            continue

        data = json.loads(content)

        if resp.status == 200:
            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)

            creds = OAuth2Credentials(
                access_token=access_token,
                client_id=G_DRIVE_CLIENT_ID,
                client_secret=G_DRIVE_CLIENT_SECRET,
                refresh_token=refresh_token,
                token_expiry=datetime.utcnow() + timedelta(seconds=expires_in),
                token_uri=TOKEN_URI,
                user_agent="GDrive-Uploader-TG-Bot/1.0"
            )
            db.set_credential(user_id, creds)
            user_auth_state.pop(user_id, None)

            await client.send_message(
                user_id,
                "**Authorized Google Drive account Successfully.**"
            )
            return
        else:
            error = data.get("error")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval += 5
                continue
            elif error == "expired_token":
                await client.send_message(user_id, "❗ **Authentication timed out.**\n__Run /auth again.__")
                user_auth_state.pop(user_id, None)
                return
            elif error == "access_denied":
                await client.send_message(user_id, "❗ **Authorization denied.**\n__Run /auth again if you change your mind.__")
                user_auth_state.pop(user_id, None)
                return
            else:
                await client.send_message(user_id, f"**ERROR:** ```{content}```")
                user_auth_state.pop(user_id, None)
                return

    await client.send_message(user_id, "❗ **Authentication timed out.**\n__Run /auth again.__")
    user_auth_state.pop(user_id, None)


def _do_token_poll(device_code):
    http = Http()
    body = (
        f"client_id={G_DRIVE_CLIENT_ID}"
        f"&client_secret={G_DRIVE_CLIENT_SECRET}"
        f"&device_code={device_code}"
        f"&grant_type=urn:ietf:params:oauth:grant-type:device_code"
    )
    resp, content = http.request(
        TOKEN_URI,
        method="POST",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    return resp, content


@Client.on_message(private & incoming & command(['revoke']))
async def _revoke(client, message):
    if db.get_credential(message.from_user.id) is None:
        await message.reply_text(
            "🔑 **You have not authenticated me to upload to any account.**\n"
            "__Send /auth to authenticate.__", quote=True
        )
    else:
        try:
            db.clear_credential(message.from_user.id)
            await message.reply_text("🔓 **Authenticated Account revoked successfully.**", quote=True)
        except Exception as e:
            await message.reply_text(f"**ERROR:** ```{e}```", quote=True)


@Client.on_message(private & incoming & command(['setfolder']))
async def _set_parent(client, message):
    if len(message.command) > 1:
        cmd_msg = message.command[1]
        if cmd_msg.lower() == "clear":
            sql.del_id(message.from_user.id)
            await message.reply_text(
                '**Custom Folder ID Cleared**\n'
                '__Use /setfolder <Folder URL> to set it back.__',
                quote=True
            )
        else:
            file_id = getIdFromUrl(cmd_msg)
            if 'NotFound' in file_id:
                await message.reply_text(
                    '❗ **Invalid Folder URL**\n__Copy the custom folder id correctly.__',
                    quote=True
                )
            else:
                sql.set_id(message.from_user.id, file_id)
                await message.reply_text(
                    f'**Custom Folder ID set Successfully**\n'
                    f'__Your custom folder id set to {file_id}. All the uploads (from now) goes here.\n'
                    'Use__ ```/setfolder clear``` __to clear the folder.__', quote=True
                )
    else:
        parent = sql.get_id(message.from_user.id)
        if parent:
            await message.reply_text(
                f'**Your custom folder id is** ```{parent.parent_id}```.',
                quote=True
            )
        else:
            await message.reply_text(
                '**You did not set any Custom Folder ID**\n'
                '__Use__ ```/setfolder {folder URL}``` __to set your custom folder ID.__',
                quote=True
            )


def getIdFromUrl(link: str):
    found = re.search(
        r'https://drive\.google\.com/[\w\?\./&=]+([-\w]{33}|(?<=/)0A[-\w]{17})', link)
    if found:
        return found.group(1)
    elif len(link.split()[-1]) == 33 or len(link.split()[-1]) == 19:
        return link.split()[-1]
    else:
        return 'NotFound'
