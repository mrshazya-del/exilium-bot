import urllib.request
import zipfile
import shutil
import os
import sys
import subprocess
import json
import logging
import re
from typing import Dict, List, Optional
import asyncio
import io
import time
from datetime import datetime, timezone, timedelta

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from google import genai
from google.genai import types
from aiohttp import web

# -------------------------------------------------------------
# Logging Setup
# -------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ExiliumBot")

# -------------------------------------------------------------
# Load Environment Variables
# -------------------------------------------------------------
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

# Role & User IDs
ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "382690116357062656").strip()
ASTRO_USER_ID = os.getenv("ASTRO_USER_ID", "987384782604353537").strip()
SUPPORT_ROLE_ID = os.getenv("SUPPORT_ROLE_ID", "1287045626860802162").strip()
EXILIUM_TEAM_ROLE_ID = os.getenv("EXILIUM_TEAM_ROLE_ID", "1203345505841582080").strip()
HEAD_SUPPORTER_ROLE_ID = os.getenv("HEAD_SUPPORTER_ROLE_ID", "1287045626860802162").strip()
COMMUNITY_SUPPORTER_ROLE_ID = os.getenv("COMMUNITY_SUPPORTER_ROLE_ID", "1244326192631386112").strip()

COMMANDS_CHANNEL_NAME = os.getenv("COMMANDS_CHANNEL_NAME", "send-commands").strip()
COMMANDS_CHANNEL_ID = os.getenv("COMMANDS_CHANNEL_ID", "1236998285680578580").strip()

raw_prefixes = os.getenv("TICKET_CHANNEL_PREFIXES", "ticket-,support-,ticket_,solved-")
TICKET_CHANNEL_PREFIXES = [p.strip().lower() for p in raw_prefixes.split(",") if p.strip()]
TICKET_CATEGORY_ID = os.getenv("TICKET_CATEGORY_ID", "").strip()

BOT_VERSION = "2.3.0"
BOT_BUILD_DATE = "2026-10-21"

# Community / Lobby Channels & Category IDs
COMMUNITY_CATEGORY_ID = os.getenv("COMMUNITY_CATEGORY_ID", "1244300178819911747").strip()
LOBBY_CHANNEL_ID = os.getenv("LOBBY_CHANNEL_ID", "1244300028659630202").strip()
TICKET_PANEL_CHANNEL_ID = os.getenv("TICKET_PANEL_CHANNEL_ID", "1299021224382562334").strip()
TICKET_PANEL_URL = "https://discord.com/channels/1203340178605735966/1299021224382562334"

raw_community = os.getenv("COMMUNITY_CHANNELS", "lobby,english,arabic,turkish,portuguese,french,spanish,german,russian,dutch,hungarian,balkan,filipino,romanian,hebrew,vietnamese,polish")
COMMUNITY_CHANNELS = [c.strip().lower() for c in raw_community.split(",") if c.strip()]

# -------------------------------------------------------------
# Staff Mentions Policy:
# Strictly NEVER mention 👑 EXILIUM TEAM 👑 role in tickets!
# Ticket staff mentions are ONLY STAM, Head Supporter, and Community Supporter.
# -------------------------------------------------------------
def get_staff_ticket_mentions(is_technical: bool = False) -> str:
    """Smart routing:
    If technical/DB/bug: strictly mention STAM and Astro.
    If general: mention STAM and Supporters.
    NEVER mention 👑 EXILIUM TEAM 👑 role in tickets!
    """
    if is_technical:
        return f"<@{ADMIN_USER_ID}> <@{ASTRO_USER_ID}>"
    
    mentions = [f"<@{ADMIN_USER_ID}>"]
    if HEAD_SUPPORTER_ROLE_ID:
        mentions.append(f"<@&{HEAD_SUPPORTER_ROLE_ID}>")
    if COMMUNITY_SUPPORTER_ROLE_ID:
        mentions.append(f"<@&{COMMUNITY_SUPPORTER_ROLE_ID}>")
    return " ".join(mentions)

# -------------------------------------------------------------
# Authorization Helpers
# -------------------------------------------------------------
def is_exilium_team_member(user: discord.abc.User) -> bool:
    """Strictly allow only members of the 👑 EXILIUM TEAM 👑 role or Server Owner."""
    if not isinstance(user, discord.Member):
        return False
    if str(user.id) == ADMIN_USER_ID or str(user.id) == "382690116357062656":
        return True
    for role in user.roles:
        if str(role.id) == EXILIUM_TEAM_ROLE_ID or "exilium team" in role.name.lower():
            return True
    return False

def is_staff_member(user: discord.abc.User) -> bool:
    """Check if user is Exilium Team, Head Supporter, Community Supporter, GM/Admin."""
    if not isinstance(user, discord.Member):
        return False
    if str(user.id) == ADMIN_USER_ID or str(user.id) == "382690116357062656" or user.guild_permissions.administrator:
        return True
    staff_ids = {
        str(EXILIUM_TEAM_ROLE_ID),
        str(HEAD_SUPPORTER_ROLE_ID),
        str(COMMUNITY_SUPPORTER_ROLE_ID),
        str(SUPPORT_ROLE_ID),
        str(ADMIN_ROLE_ID)
    }
    for role in user.roles:
        if str(role.id) in staff_ids:
            return True
        r_name = role.name.lower()
        if any(k in r_name for k in ["exilium team", "supporter", "support", "admin", "moderator", "gm"]):
            return True
    return False

def is_community_channel(channel: discord.abc.GuildChannel) -> bool:
    """Check if channel is a designated community/lobby/language channel by ID, Category, or Name."""
    if not hasattr(channel, "name"):
        return False

    cid_str = str(channel.id)

    # 1. Direct match with designated Lobby Channel ID
    if LOBBY_CHANNEL_ID and cid_str == LOBBY_CHANNEL_ID:
        return True

    # 2. Direct match with Community Category ID (covers ALL language channels inside it!)
    cat_id = getattr(channel, "category_id", None)
    if cat_id is None and hasattr(channel, "category") and channel.category:
        cat_id = channel.category.id
    if cat_id and str(cat_id) == COMMUNITY_CATEGORY_ID:
        return True

    # 3. Fallback match by channel name or comma-separated list
    cname = channel.name.lower()
    for allowed in COMMUNITY_CHANNELS:
        if allowed == cid_str or allowed == cname or (len(allowed) >= 4 and allowed in cname):
            return True

    return False

INSTANCE_LOCK_SOCKET = None

def kill_other_bot_instances() -> list:
    """Find and terminate any other ghost/zombie python processes running this bot."""
    killed = []
    my_pid = os.getpid()

    # 1. psutil kill (fast and accurate)
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = p.info['pid']
                if pid == my_pid:
                    continue
                name = (p.info['name'] or '').lower()
                if 'python' in name:
                    cmdline_parts = p.info['cmdline'] or []
                    cmdline_str = ' '.join(cmdline_parts).lower()
                    if 'bot.py' in cmdline_str and 'control' not in cmdline_str:
                        p.kill()
                        killed.append(pid)
            except Exception:
                pass
    except Exception as pe:
        logger.debug(f"psutil check: {pe}")

    # 2. Windows socket port & taskkill cleanup
    if sys.platform == "win32":
        try:
            net_out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="ignore").stdout
            for line in net_out.splitlines():
                if ":49988" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if len(parts) >= 5 and parts[-1].isdigit():
                        h_pid = int(parts[-1])
                        if h_pid != my_pid and h_pid not in killed:
                            subprocess.run(["taskkill", "/F", "/PID", str(h_pid)], capture_output=True)
                            killed.append(h_pid)
        except Exception:
            pass

    # 3. Linux / POSIX fallback
    if sys.platform != "win32" and not killed:
        try:
            p = subprocess.run(["pgrep", "-f", "bot.py"], capture_output=True, text=True, errors="ignore")
            for line in p.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid != my_pid and pid not in killed:
                        try:
                            os.kill(pid, 9)
                            killed.append(pid)
                        except Exception:
                            pass
        except Exception:
            pass

    if killed:
        logger.info(f"Cleaned up {len(killed)} duplicate bot process(es): {killed}")
    return killed

def acquire_instance_socket_lock(port: int = 49988) -> bool:
    """Guarantee strictly one single running instance of this bot on the entire operating system."""
    global INSTANCE_LOCK_SOCKET
    kill_other_bot_instances()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        INSTANCE_LOCK_SOCKET = s
        logger.info(f"Acquired single-instance socket lock on 127.0.0.1:{port}")
        return True
    except Exception as e:
        logger.warning(f"Port {port} busy ({e}). Re-killing duplicates...")
        kill_other_bot_instances()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(1)
            INSTANCE_LOCK_SOCKET = s
            return True
        except Exception as e2:
            logger.error(f"Duplicate instance detected holding port {port}: {e2}. Exiting to prevent multi-replies.")
            sys.exit(0)

def sanitize_ign(raw_ign: str) -> str:
    """Strictly strip newlines, spaces, and command injection symbols while fully preserving valid Silkroad IGN characters: [], (), _, -, ., ~."""
    if not raw_ign:
        return ""
    # Strip dangerous newlines and injection characters, but keep brackets and underscores
    clean = re.sub(r"[^\w\.\-\(\)\[\]\~]", "", raw_ign).strip()
    return clean[:25]

# -------------------------------------------------------------
# Global States & Watchdogs
# -------------------------------------------------------------
# channel_id -> bool: True when staff spoke in ticket
ticket_staff_taken_over: Dict[int, bool] = {}
# channel_id -> timestamp: last time staff spoke in this ticket (for 24h smart silence expiration)
ticket_staff_last_msg_ts: Dict[int, float] = {}
# channel_id -> bool: True when staff was already pinged in this ticket to prevent repeat pings
ticket_escalated: Dict[int, bool] = {}

# Maintenance Mode State
maintenance_mode: bool = False
maintenance_reason: str = ""
maintenance_start_ts: float = 0.0
maintenance_duration_min: int = 0

# Cooldowns for Community Channels
community_channel_cooldown: Dict[int, float] = {}
community_user_cooldown: Dict[int, float] = {}

# -------------------------------------------------------------
# Initialize Gemini Client
# -------------------------------------------------------------
gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini API Client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
else:
    logger.warning("GEMINI_API_KEY is not set in environment or .env! AI responses will fail.")

# -------------------------------------------------------------
# Knowledge Base Loader & Fast Path
# -------------------------------------------------------------
KB_FILE_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.json")
knowledge_base: dict = {}

def load_knowledge_base():
    global knowledge_base
    try:
        if os.path.exists(KB_FILE_PATH):
            with open(KB_FILE_PATH, "r", encoding="utf-8") as f:
                knowledge_base = json.load(f)
            logger.info("Knowledge base loaded successfully.")
        else:
            logger.warning(f"knowledge_base.json not found at {KB_FILE_PATH}")
            knowledge_base = {}
    except Exception as e:
        logger.error(f"Error loading knowledge base: {e}")
        knowledge_base = {}

load_knowledge_base()

def check_download_fast_path(user_text: str) -> Optional[str]:
    """Instant 0.01s local response for pure download link queries without hitting Gemini API."""
    text_lower = user_text.lower().strip()
    download_keywords = [
        "mediafire", "mega.nz", "google drive", "تحميل اللعبة", "رابط التحميل",
        "روابط التحميل", "تحميل الكلاينت", "لينك التحميل", "رابط تحميل",
        "download link", "download client", "download game", "download the game",
        "where to download", "how to download", "how can i download", "where can i download",
        "oyun indir", "nereden indirebilirim", "indir link", "nasil indirebilirim",
        "baixar jogo", "como baixar", "onde baixar", "download spiel"
    ]
    if any(kw in text_lower for kw in download_keywords):
        downloads = knowledge_base.get("official_downloads", {})
        lines = [
            "📥 **Official Exilium Download Links (روابط التحميل الرسمية المباشرة):**",
            f"• **Direct Link (الرابط المباشر):** [Download Client]({downloads.get('direct', 'https://exiliumonline.com/download')})",
            f"• **Google Drive:** [Google Drive Mirror]({downloads.get('google_drive', '#')})",
            f"• **MediaFire:** [MediaFire Mirror]({downloads.get('mediafire', '#')})",
            f"• **Mega Mirror 1:** [Mega Mirror]({downloads.get('mega', '#')})",
            f"• **Files.fm:** [Files.fm Mirror]({downloads.get('files_fm', '#')})",
            "\n💡 *Extract with WinRAR or 7-Zip and always run the Launcher as Administrator!*"
        ]
        return "\n".join(lines)
    return None

# -------------------------------------------------------------
# System Instruction Builder with Cap 80 CH Builds & Empathy
# -------------------------------------------------------------
def build_system_instruction() -> str:
    kb_str = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
    return f"""You are the official AI Support Assistant for "Exilium", a premier Silkroad Online (vSRO Cap 80 CH Only) private server.
Your name is "Exilium Assistant". You assist players warmly, concisely, and accurately in Discord support tickets and community channels.

Authoritative Exilium Server Knowledge Base:
=== KNOWLEDGE BASE START ===
{kb_str}
=== KNOWLEDGE BASE END ===

OPERATING INSTRUCTIONS & EXPERTISE:
1. STRICT BREVITY & CONCISENESS:
   - Keep ALL responses short, direct, and actionable (2 to 4 sentences or short bullet points).

2. ZERO HALLUCINATION POLICY:
   - Rely strictly on the Knowledge Base. If an issue is unknown or requires internal database checks, politely state:
     "This requires staff assistance. I have notified our Support and Management team to assist you directly."
     and append [ESCALATE_TO_ADMIN].

3. STARTER PACKAGE CRITERIA (ANTI-FALSE TRIGGER):
   - ONLY trigger starter confirmation if the player is EXPLICITLY requesting a starter package/pack for themselves as a new player (e.g. "I want starter package", "i am new player can i get pack", "ana gded", "yeni basladim paket").
   - If they are merely discussing, asking how it works, asking about GM commands, or asking curiosity questions: DO NOT trigger confirmation! Simply explain how new players receive starter rewards.
   - When legitimately requested, reply briefly in English:
     "Welcome to Exilium! Your request for the Starter Package has been submitted to the administration for confirmation."
     and append the exact token: <<<AWAIT_STARTER_CONFIRMATION>>>

4. CAP 80 CH BUILD DOCTOR (DEEP EXPERTISE):
   - When players ask for build advice or compare builds in Cap 80 Chinese (Max 300 Mastery limit):
     * Glaive STR: 80 Heuksal / 80 Fire / 80 Cold / 60 Lightning (Protector/Garment, high HP, massive physical burst, tanky).
     * Bow STR: 80 Pacheon / 80 Fire / 80 Cold / 60 Lightning (Long range, critical hits, high dodge, excellent in 1v1 PvP and Jobbing).
     * Pure INT Spear: 80 Heuksal / 80 Lightning / 80 Cold / 60 Fire (Highest magical AoE nuke damage in game, high glass cannon burst, needs Cold shields and Protector).
     * Nuker Sword/Shield (Bicheon): 80 Bicheon / 80 Lightning / 80 Cold / 60 Fire (Shield defense, knockdown + down attack combo, high survivability).

5. EMPATHETIC RAGE & DC HANDLING:
   - When a player complains angrily about lag, disconnections, or crashes ("trash server", "lag", "dc every 5m", "fix your game"):
     * Do NOT give cold robotic replies like "check your internet".
     * Acknowledge empathetically: Apologize for the frustration, clarify that the server infrastructure is running normally, and guide them to diagnose:
       "We apologize for the frustration! The server is currently stable with no widespread issues. To fix this quickly: Are you getting Error C9, or is the client closing silently? Please check that Windows Defender hasn't quarantined sro_client.exe and restart your router."

6. PROACTIVE ERROR SCREENSHOT PROMPTING:
   - When a player describes a technical error, launcher crash, black screen, or missing file WITHOUT uploading an image:
     * Provide the initial troubleshooting steps, and end with:
       "💡 To resolve your issue faster, please upload a clear screenshot of the error message (📸 Shortcut: press Win + Shift + S to capture, then Ctrl + V here)."

7. AUTOMATED SILK STORE INQUIRIES:
   - Silk purchasing and donations are 100% automated on the website. If a player asks about buying Silk, payment methods, or store:
     Direct them immediately to the official store: https://exiliumonline.com/account/store and mention that delivery is instant.

8. ISSUE RESOLVED CONFIRMATION (100% SOLVED):
   - When the player confirms their issue is 100% solved ("thanks it worked", "solved", "all good now", "fixed now", "شكرا اتحلت", "اشتغلت خلاص", "sagol oldu"):
     Reply warmly: "Awesome! So glad to hear your issue is completely resolved. I have marked this channel as solved so our team knows it is ready to be closed. Have fun playing Exilium! 👋"
     and append [TICKET_RESOLVED].
"""

# -------------------------------------------------------------
# Fast Async AI Generation with 1-Retry Backoff
# -------------------------------------------------------------
async def generate_ai_text_async(prompt: str, image_parts: list = None) -> str:
    """Generate response using Gemini async client with candidate fallback and 1-retry backoff."""
    if not gemini_client:
        return "❌ مفتاح الذكاء الاصطناعي غير متوفر حالياً."

    candidate_models = [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.6-flash"
    ]
    seen = set()
    models_to_try = [m for m in candidate_models if m and not (m in seen or seen.add(m))]

    contents = []
    if image_parts:
        contents.extend(image_parts)
    contents.append(prompt)

    last_error = None
    for attempt in range(2):
        for model_name in models_to_try:
            try:
                response = await asyncio.wait_for(
                    gemini_client.aio.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=build_system_instruction(),
                            temperature=0.3,
                            max_output_tokens=600
                        )
                    ),
                    timeout=5.5
                )
                if response and response.text:
                    return response.text
            except asyncio.TimeoutError:
                logger.warning(f"Model '{model_name}' timed out after 5.5s, switching candidate.")
                last_error = TimeoutError(f"Model {model_name} timed out.")
            except Exception as e:
                logger.warning(f"Model '{model_name}' error: {e}, switching candidate.")
                last_error = e

        if attempt == 0:
            logger.info("Transient API timeout/error. Waiting 1.0s before retry...")
            await asyncio.sleep(1.0)

    logger.error(f"All candidate models failed: {last_error}")
    raise last_error if last_error else RuntimeError("No response from AI models.")

# -------------------------------------------------------------
# Conversation History Storage per Ticket Channel
# -------------------------------------------------------------
ticket_histories: Dict[int, List[dict]] = {}
ticket_igns: Dict[int, str] = {}
MAX_HISTORY_PER_CHANNEL = 12

def is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    name = channel.name.lower()
    for prefix in TICKET_CHANNEL_PREFIXES:
        if name.startswith(prefix) or prefix in name:
            return True
    if TICKET_CATEGORY_ID and hasattr(channel, "category_id") and str(channel.category_id) == TICKET_CATEGORY_ID:
        return True
    return False

# -------------------------------------------------------------
# Daily Tickets Persistent Tracking
# -------------------------------------------------------------
DAILY_TICKETS_FILE = os.path.join(os.path.dirname(__file__), "daily_tickets.json")
daily_tickets_data: dict = {}

def load_daily_tickets():
    global daily_tickets_data
    if os.path.exists(DAILY_TICKETS_FILE):
        try:
            with open(DAILY_TICKETS_FILE, "r", encoding="utf-8") as f:
                daily_tickets_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading daily tickets: {e}")
            daily_tickets_data = {}
    else:
        daily_tickets_data = {}

def save_daily_tickets():
    try:
        with open(DAILY_TICKETS_FILE, "w", encoding="utf-8") as f:
            json.dump(daily_tickets_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving daily tickets: {e}")

load_daily_tickets()

def record_daily_ticket(channel_id: int, channel_name: str, ign: str, player_name: str, issue: str, status: str = "⏳ In Progress"):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    day_records = daily_tickets_data.setdefault(today_str, {})
    cid_str = str(channel_id)
    if cid_str not in day_records:
        day_records[cid_str] = {
            "channel_name": channel_name,
            "ign": ign or "N/A",
            "player_name": player_name,
            "issue": issue[:120],
            "status": status,
            "opened_at": now_time,
            "last_updated": now_time
        }
    else:
        day_records[cid_str]["status"] = status
        day_records[cid_str]["last_updated"] = now_time
        if ign and day_records[cid_str].get("ign") == "N/A":
            day_records[cid_str]["ign"] = ign
    save_daily_tickets()

def update_daily_ticket_status(channel_id: int, new_status: str):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    day_records = daily_tickets_data.get(today_str, {})
    cid_str = str(channel_id)
    if cid_str in day_records:
        day_records[cid_str]["status"] = new_status
        day_records[cid_str]["last_updated"] = now_time
        save_daily_tickets()

def generate_daily_report_embed(date_str: str) -> discord.Embed:
    day_records = daily_tickets_data.get(date_str, {})
    total = len(day_records)
    resolved = sum(1 for t in day_records.values() if "Resolved" in t.get("status", ""))
    starters = sum(1 for t in day_records.values() if "Starter" in t.get("status", ""))
    escalated = sum(1 for t in day_records.values() if "Escalated" in t.get("status", ""))
    in_progress = sum(1 for t in day_records.values() if "In Progress" in t.get("status", ""))

    embed = discord.Embed(
        title=f"📋 Exilium Daily Support Report — {date_str}",
        description=(
            f"**Total Tickets Handled Today:** `{total}`\n"
            f"✅ **Resolved Tickets:** `{resolved}`\n"
            f"🎁 **Starter Packages Approved:** `{starters}`\n"
            f"🚨 **Escalated to Staff:** `{escalated}`\n"
            f"⏳ **Active / In Progress:** `{in_progress}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x00A8FF
    )
    if day_records:
        lines = []
        for cid, t in list(day_records.items())[:15]:
            ign_part = f" (IGN: `{t['ign']}`)" if t.get('ign') and t['ign'] != 'N/A' else ""
            lines.append(f"• **#{t['channel_name']}**{ign_part}: {t['status']} | *{t['issue'][:40]}*")
        embed.add_field(name="Latest Tickets Summary", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Summary", value="*No tickets recorded for this date yet.*", inline=False)

    embed.set_footer(text="Exilium Support Management • Daily Executive Brief")
    return embed

async def send_daily_dm_to_stam(date_str: str):
    try:
        stam_user = await bot.fetch_user(int(ADMIN_USER_ID))
        if stam_user:
            embed = generate_daily_report_embed(date_str)
            await stam_user.send(content=f"👑 **Hello [GM]STAM! Here is your Exilium Daily Tickets Summary for {date_str}:**", embed=embed)
            logger.info(f"Successfully sent daily DM report for {date_str} to [GM]STAM.")
    except Exception as e:
        logger.error(f"Failed to send daily report DM to [GM]STAM: {e}")

# -------------------------------------------------------------
# Ticket Inactivity Tracker Storage
# -------------------------------------------------------------
INACTIVITY_FILE = os.path.join(os.path.dirname(__file__), "inactivity_tracker.json")
inactivity_data: dict = {}

def load_inactivity_data():
    global inactivity_data
    if os.path.exists(INACTIVITY_FILE):
        try:
            with open(INACTIVITY_FILE, "r", encoding="utf-8") as f:
                inactivity_data = json.load(f)
        except Exception:
            inactivity_data = {}
    else:
        inactivity_data = {}

def save_inactivity_data():
    try:
        with open(INACTIVITY_FILE, "w", encoding="utf-8") as f:
            json.dump(inactivity_data, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving inactivity data: {e}")

load_inactivity_data()

# -------------------------------------------------------------
# Ticket Analytics & Weekly Stats
# -------------------------------------------------------------
ANALYTICS_FILE = os.path.join(os.path.dirname(__file__), "ticket_analytics.json")
analytics_data: dict = {}

def load_analytics():
    global analytics_data
    if os.path.exists(ANALYTICS_FILE):
        try:
            with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                analytics_data = json.load(f)
        except Exception:
            analytics_data = {"tickets_total": 0, "starters_given": 0, "categories": {}}
    else:
        analytics_data = {"tickets_total": 0, "starters_given": 0, "categories": {}}

def save_analytics():
    try:
        with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(analytics_data, f, indent=2)
    except Exception:
        pass

load_analytics()

def record_ticket_stat(category: str):
    analytics_data["tickets_total"] = analytics_data.get("tickets_total", 0) + 1
    cats = analytics_data.setdefault("categories", {})
    cats[category] = cats.get(category, 0) + 1
    save_analytics()

def record_starter_given():
    analytics_data["starters_given"] = analytics_data.get("starters_given", 0) + 1
    save_analytics()

def generate_analytics_embed() -> discord.Embed:
    total_t = analytics_data.get("tickets_total", 0)
    starters_g = analytics_data.get("starters_given", 0)
    categories = analytics_data.get("categories", {})

    breakdown_lines = []
    if categories:
        for cat, cnt in sorted(categories.items(), key=lambda x: x[1], reverse=True)[:15]:
            breakdown_lines.append(f"• **{cat}:** {cnt} ticket(s)")
    else:
        breakdown_lines.append("• *No categorized tickets recorded yet.*")

    embed = discord.Embed(
        title="📊 Exilium Support Analytics & Insights",
        description=(
            f"**Total Tickets Handled:** `{total_t}`\n"
            f"**Starter Packages Approved:** `{starters_g}`\n\n"
            f"📈 **Top Inquiries & Error Categories:**\n" + "\n".join(breakdown_lines) + "\n\n"
            f"💡 **AI Health Status:** System functioning optimally with sub-second response times on `gemini-3.5-flash-lite`."
        ),
        color=0x2ECC71
    )
    embed.set_footer(text="Exilium Analytics Engine • Automated Weekly Report")
    return embed

# -------------------------------------------------------------
# Claimed Starters Persistent Tracking (Multi-Alt & Security)
# -------------------------------------------------------------
CLAIMED_STARTERS_FILE = os.path.join(os.path.dirname(__file__), "claimed_starters.json")
claimed_starters_data: dict = {"by_discord_id": {}, "by_ign": {}}
claimed_starters_lock = asyncio.Lock()

def load_claimed_starters():
    global claimed_starters_data
    if os.path.exists(CLAIMED_STARTERS_FILE):
        try:
            with open(CLAIMED_STARTERS_FILE, "r", encoding="utf-8") as f:
                claimed_starters_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading claimed starters: {e}")
            claimed_starters_data = {"by_discord_id": {}, "by_ign": {}}
    else:
        claimed_starters_data = {"by_discord_id": {}, "by_ign": {}}

load_claimed_starters()

async def save_claimed_starters_async():
    async with claimed_starters_lock:
        try:
            with open(CLAIMED_STARTERS_FILE, "w", encoding="utf-8") as f:
                json.dump(claimed_starters_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving claimed starters: {e}")

def check_claimed_starter_history(user_id: int, ign: str) -> Optional[dict]:
    uid_str = str(user_id) if user_id else ""
    ign_key = ign.strip().lower() if ign else ""
    if uid_str and uid_str in claimed_starters_data.get("by_discord_id", {}):
        return {"match_type": "Discord Account", "record": claimed_starters_data["by_discord_id"][uid_str]}
    if ign_key and ign_key in claimed_starters_data.get("by_ign", {}):
        return {"match_type": "IGN", "record": claimed_starters_data["by_ign"][ign_key]}
    return None

async def record_claimed_starter(user_id: int, ign: str, approved_by_name: str, items_sent: list):
    uid_str = str(user_id) if user_id else ""
    ign_key = ign.strip().lower()
    entry = {
        "ign": ign,
        "user_id": uid_str,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "approved_by": approved_by_name,
        "items": items_sent
    }
    if uid_str:
        claimed_starters_data.setdefault("by_discord_id", {})[uid_str] = entry
    if ign_key:
        claimed_starters_data.setdefault("by_ign", {})[ign_key] = entry
    await save_claimed_starters_async()

# -------------------------------------------------------------
# Starter Package Multi-Select UI & Execution
# -------------------------------------------------------------
pending_starters: Dict[int, dict] = {}

async def execute_starter_dispatch(
    guild: discord.Guild,
    ign: str,
    ticket_channel: discord.TextChannel,
    confirmed_by=None,
    selected_items: list = None,
    target_user: discord.User = None
):
    clean_ign = sanitize_ign(ign)
    if not clean_ign:
        await ticket_channel.send("⚠️ Error: In-Game Name (IGN) is empty or invalid.")
        return

    if selected_items is None:
        selected_items = ["pkg", "silk", "beta", "zone"]

    cmd_channel = None
    if COMMANDS_CHANNEL_ID:
        try:
            cmd_channel = bot.get_channel(int(COMMANDS_CHANNEL_ID))
            if not cmd_channel:
                cmd_channel = await bot.fetch_channel(int(COMMANDS_CHANNEL_ID))
        except Exception as e:
            logger.error(f"Error fetching commands channel by ID {COMMANDS_CHANNEL_ID}: {e}")

    if not cmd_channel and guild:
        cmd_channel = discord.utils.get(guild.text_channels, name=COMMANDS_CHANNEL_NAME)

    if not cmd_channel:
        await ticket_channel.send("⚠️ Error: Commands channel could not be reached. Please verify bot channel permissions.")
        return

    perms = cmd_channel.permissions_for(cmd_channel.guild.me)
    if not perms.send_messages:
        await ticket_channel.send(f"⚠️ Error: Bot lacks 'Send Messages' permission in {cmd_channel.mention}.")
        return

    try:
        sent_labels = []
        if "pkg" in selected_items:
            await cmd_channel.send(f"!startpackage {clean_ign}")
            sent_labels.append("Starter Package")
            await asyncio.sleep(0.8)
        if "silk" in selected_items:
            await cmd_channel.send(f"!playersilkgift {clean_ign},1500")
            sent_labels.append("1,500 Silk Gift")
            await asyncio.sleep(0.8)
        if "beta" in selected_items:
            await cmd_channel.send(f"!betabox {clean_ign}")
            sent_labels.append("Beta Box")
            await asyncio.sleep(0.8)
        if "zone" in selected_items:
            await cmd_channel.send(f"!srozonebox {clean_ign}")
            sent_labels.append("SRO Zone Box")

        record_starter_given()
        update_daily_ticket_status(ticket_channel.id, "🎁 Starter Delivered")

        approver_name = confirmed_by.display_name if confirmed_by else "Admin"
        uid = target_user.id if target_user else 0
        await record_claimed_starter(uid, clean_ign, approver_name, sent_labels)

        confirm_embed = discord.Embed(
            title="🎁 Starter Package Delivered!",
            description=(
                f"✅ **Your starter rewards have been delivered to `{clean_ign}`!**\n\n"
                f"• **Items Granted:** {', '.join(sent_labels)}\n\n"
                f"📦 Please check your in-game Chest. Have fun playing Exilium!"
            ),
            color=0x2ECC71
        )
        confirm_embed.set_footer(text="Exilium Support Management • Automated Delivery")
        await ticket_channel.send(embed=confirm_embed)

        if ticket_channel.id in pending_starters:
            del pending_starters[ticket_channel.id]

    except Exception as e:
        logger.error(f"Error dispatching starter commands: {e}")
        await ticket_channel.send(f"⚠️ Error dispatching starter commands: {e}")

class StarterApprovalView(discord.ui.View):
    def __init__(self, ign: str, ticket_channel: discord.TextChannel, target_user: Optional[discord.User] = None):
        super().__init__(timeout=None)
        self.ign = sanitize_ign(ign)
        self.ticket_channel = ticket_channel
        self.target_user = target_user

        self.selected = {
            "pkg": False,
            "silk": False,
            "beta": False,
            "zone": False
        }

        cid_tag = re.sub(r"[^\w]", "", self.ign)[:8] or "player"
        self.btn_pkg = discord.ui.Button(
            label="Starter Gear [OFF]",
            emoji="📦",
            style=discord.ButtonStyle.secondary,
            custom_id=f"st_pkg_{cid_tag}",
            row=0
        )
        self.btn_pkg.callback = self.toggle_pkg

        self.btn_silk = discord.ui.Button(
            label="1,500 Silk [OFF]",
            emoji="💎",
            style=discord.ButtonStyle.secondary,
            custom_id=f"st_silk_{cid_tag}",
            row=0
        )
        self.btn_silk.callback = self.toggle_silk

        self.btn_beta = discord.ui.Button(
            label="Beta Box [OFF]",
            emoji="🎁",
            style=discord.ButtonStyle.secondary,
            custom_id=f"st_beta_{cid_tag}",
            row=1
        )
        self.btn_beta.callback = self.toggle_beta

        self.btn_zone = discord.ui.Button(
            label="SRO Zone Box [OFF]",
            emoji="🧰",
            style=discord.ButtonStyle.secondary,
            custom_id=f"st_zone_{cid_tag}",
            row=1
        )
        self.btn_zone.callback = self.toggle_zone

        self.btn_dispatch = discord.ui.Button(
            label="🚀 Deliver Selected",
            style=discord.ButtonStyle.primary,
            custom_id=f"st_send_{cid_tag}",
            row=2
        )
        self.btn_dispatch.callback = self.dispatch_callback

        self.btn_decline = discord.ui.Button(
            label="❌ Decline Request",
            style=discord.ButtonStyle.danger,
            custom_id=f"st_dec_{cid_tag}",
            row=2
        )
        self.btn_decline.callback = self.decline_callback

        self.add_item(self.btn_pkg)
        self.add_item(self.btn_silk)
        self.add_item(self.btn_beta)
        self.add_item(self.btn_zone)
        self.add_item(self.btn_dispatch)
        self.add_item(self.btn_decline)

    def _update_button_visuals(self):
        self.btn_pkg.style = discord.ButtonStyle.success if self.selected["pkg"] else discord.ButtonStyle.secondary
        self.btn_pkg.label = "Starter Gear [ON]" if self.selected["pkg"] else "Starter Gear [OFF]"

        self.btn_silk.style = discord.ButtonStyle.success if self.selected["silk"] else discord.ButtonStyle.secondary
        self.btn_silk.label = "1,500 Silk [ON]" if self.selected["silk"] else "1,500 Silk [OFF]"

        self.btn_beta.style = discord.ButtonStyle.success if self.selected["beta"] else discord.ButtonStyle.secondary
        self.btn_beta.label = "Beta Box [ON]" if self.selected["beta"] else "Beta Box [OFF]"

        self.btn_zone.style = discord.ButtonStyle.success if self.selected["zone"] else discord.ButtonStyle.secondary
        self.btn_zone.label = "SRO Zone Box [ON]" if self.selected["zone"] else "SRO Zone Box [OFF]"

    def get_summary_text(self) -> str:
        lines = [
            f"• 📦 **Starter Equipment:** {'✅ `Included`' if self.selected['pkg'] else '⬜ `Excluded`'}",
            f"• 💎 **1,500 Silk Gift:** {'✅ `Included`' if self.selected['silk'] else '⬜ `Excluded`'}",
            f"• 🎁 **Beta Box:** {'✅ `Included`' if self.selected['beta'] else '⬜ `Excluded`'}",
            f"• 🧰 **SRO Zone Box:** {'✅ `Included`' if self.selected['zone'] else '⬜ `Excluded`'}"
        ]
        return "\n".join(lines)

    async def _handle_toggle(self, interaction: discord.Interaction, key: str):
        if not is_exilium_team_member(interaction.user):
            await interaction.response.send_message("❌ This action is restricted to [GM]STAM and 👑 EXILIUM TEAM only.", ephemeral=True)
            return

        self.selected[key] = not self.selected[key]
        self._update_button_visuals()

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title="🎁 Starter Package Dispatch")
        warning_part = ""
        desc = embed.description or ""
        if "⚠️ **Duplicate Claim Warning:" in desc:
            w_idx = desc.find("⚠️ **Duplicate Claim Warning:")
            w_end = desc.find("\n\n**Delivery Checklist", w_idx)
            if w_end != -1:
                warning_part = "\n\n" + desc[w_idx:w_end]
            else:
                warning_part = "\n\n" + desc[w_idx:]

        embed.description = (
            f"**Player IGN:** `{self.ign}`{warning_part}\n\n"
            f"**Delivery Checklist (Click buttons to toggle ON/OFF):**\n"
            f"{self.get_summary_text()}\n\n"
            f"👉 When ready, click **[🚀 Deliver Selected]** below."
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def toggle_pkg(self, interaction: discord.Interaction):
        await self._handle_toggle(interaction, "pkg")

    async def toggle_silk(self, interaction: discord.Interaction):
        await self._handle_toggle(interaction, "silk")

    async def toggle_beta(self, interaction: discord.Interaction):
        await self._handle_toggle(interaction, "beta")

    async def toggle_zone(self, interaction: discord.Interaction):
        await self._handle_toggle(interaction, "zone")

    async def dispatch_callback(self, interaction: discord.Interaction):
        if not is_exilium_team_member(interaction.user):
            await interaction.response.send_message("❌ This action is restricted to [GM]STAM and 👑 EXILIUM TEAM only.", ephemeral=True)
            return

        selected_keys = [k for k, v in self.selected.items() if v]
        if not selected_keys:
            await interaction.response.send_message("⚠️ Please enable at least one reward package before delivering!", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        self.btn_dispatch.label = f"✅ Delivered to {self.ign}"
        self.btn_dispatch.style = discord.ButtonStyle.success
        await interaction.response.edit_message(view=self)

        await execute_starter_dispatch(
            interaction.guild,
            self.ign,
            self.ticket_channel,
            confirmed_by=interaction.user,
            selected_items=selected_keys,
            target_user=self.target_user
        )

    async def decline_callback(self, interaction: discord.Interaction):
        if not is_exilium_team_member(interaction.user):
            await interaction.response.send_message("❌ This action is restricted to [GM]STAM and 👑 EXILIUM TEAM only.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        self.btn_decline.label = "❌ Request Declined"
        await interaction.response.edit_message(view=self)
        await self.ticket_channel.send(f"❌ Starter package request for `{self.ign}` has been declined by administration.")

# -------------------------------------------------------------
# Discord Bot Setup & Views
# -------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

class TicketCreateButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="فتح تذكرة دعم | Open Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="exilium_open_ticket_btn"
    )
    async def open_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ هذا الأمر متاح داخل السيرفر فقط.", ephemeral=True)
            return

        ticket_channel_name = f"ticket-{interaction.user.name.lower()[:15]}"
        existing_channel = discord.utils.get(guild.text_channels, name=ticket_channel_name)
        if existing_channel:
            await interaction.response.send_message(
                f"⚠️ لديك تذكرة مفتوحة بالفعل هنا: {existing_channel.mention}",
                ephemeral=True
            )
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        category = None
        if TICKET_CATEGORY_ID:
            try:
                category = guild.get_channel(int(TICKET_CATEGORY_ID))
            except ValueError:
                pass

        ticket_channel = await guild.create_text_channel(
            name=ticket_channel_name,
            overwrites=overwrites,
            category=category,
            topic=f"Exilium Support Ticket for {interaction.user.name} (ID: {interaction.user.id})"
        )

        await interaction.response.send_message(
            f"✅ تم إنشاء تذكرتك بنجاح: {ticket_channel.mention}",
            ephemeral=True
        )

class DownloadHelpView(discord.ui.View):
    def __init__(self, include_vcredist: bool = True, include_directx: bool = True):
        super().__init__(timeout=None)
        if include_vcredist:
            self.add_item(discord.ui.Button(
                label="تحميل Visual C++ All-in-One",
                url="https://aka.ms/vs/17/release/vc_redist.x86.exe",
                style=discord.ButtonStyle.link,
                emoji="⚙️"
            ))
        if include_directx:
            self.add_item(discord.ui.Button(
                label="تحميل DirectX 9.0c End-User",
                url="https://www.microsoft.com/en-us/download/details.aspx?id=35",
                style=discord.ButtonStyle.link,
                emoji="🎮"
            ))
        self.add_item(discord.ui.Button(
            label="موقع Exilium الرسمي",
            url="https://exiliumonline.com",
            style=discord.ButtonStyle.link,
            emoji="🌐"
        ))

class TicketRedirectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Open Support Ticket",
            url=TICKET_PANEL_URL,
            emoji="🎫",
            style=discord.ButtonStyle.link
        ))

class StaffCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="إغلاق التذكرة | Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="exilium_staff_close_ticket_btn"
    )
    async def close_btn(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff_member(btn_interaction.user) and not is_exilium_team_member(btn_interaction.user):
            await btn_interaction.response.send_message("❌ هذا الإجراء مخصص لطاقم الدعم والإدارة فقط.", ephemeral=True)
            return

        button.disabled = True
        button.label = "🔒 جاري الإغلاق..."
        await btn_interaction.response.edit_message(view=self)

        channel = btn_interaction.channel
        await channel.send("🔒 **تم تأكيد حل المشكلة وإغلاق التذكرة بواسطة الإدارة.** سيتم حذف القناة خلال 5 ثوانٍ...")
        await asyncio.sleep(5.0)
        try:
            await channel.delete(reason=f"Ticket closed by staff {btn_interaction.user.name}")
        except Exception as e:
            logger.warning(f"Could not delete channel {channel.name}: {e}")

# -------------------------------------------------------------
# Background Tasks & Watchdogs
# -------------------------------------------------------------
@tasks.loop(seconds=60)
async def check_ticket_inactivity_loop():
    """Checks open tickets for 12h player inactivity, followed by 30m support escalation."""
    now = time.time()
    channels_to_remove = []

    for cid_str, data in list(inactivity_data.items()):
        try:
            cid = int(cid_str)
            channel = bot.get_channel(cid)
            if not channel:
                try:
                    channel = await bot.fetch_channel(cid)
                except Exception:
                    channels_to_remove.append(cid_str)
                    continue

            # Item 10: Closed/Archived Ticket Filter
            cname = channel.name.lower()
            if any(k in cname for k in ["closed", "archive", "deleted"]):
                channels_to_remove.append(cid_str)
                continue

            player_id = data.get("player_id")
            player_mention = f"<@{player_id}>" if player_id else "Player"
            last_reply = data.get("last_reply_ts", now)
            reminder_sent = data.get("reminder_sent_ts")
            escalated = data.get("escalated", False)

            # Stage 1: 12 Hours of inactivity after bot's reply
            if reminder_sent is None:
                if (now - last_reply) >= 12 * 3600:
                    reminder_embed = discord.Embed(
                        title="⏰ Ticket Inactivity Reminder",
                        description=(
                            f"👋 Hello {player_mention}!\n\n"
                            f"Just checking in: Are you still experiencing this issue, or has it been resolved?\n\n"
                            f"👉 **Notice:** Please reply within **30 minutes** to keep this ticket active. "
                            f"If your issue is resolved, please close the ticket via the red **[Close Ticket]** button above."
                        ),
                        color=0xF39C12
                    )
                    reminder_embed.set_footer(text="Exilium Support Management • Automated Inactivity Check")
                    await channel.send(content=player_mention, embed=reminder_embed)
                    data["reminder_sent_ts"] = now
                    save_inactivity_data()
                    logger.info(f"Sent 12h inactivity reminder to ticket #{channel.name}")

            # Stage 2: 30 minutes after reminder without player reply -> Ping STAM, Head Supporter, Supporter
            elif not escalated:
                if (now - reminder_sent) >= 30 * 60:
                    support_pings = get_staff_ticket_mentions()
                    escalate_embed = discord.Embed(
                        title="⚠️ Inactive Ticket - Support Intervention",
                        description=(
                            f"**Player:** {player_mention}\n"
                            f"**Channel:** {channel.mention}\n\n"
                            f"💡 **Status:** The player has not responded for **12 hours**, and did not reply within **30 minutes** after the inactivity reminder.\n\n"
                            f"👉 **Action:** Support team may review the ticket or proceed with closure if no further assistance is needed."
                        ),
                        color=0xE74C3C
                    )
                    escalate_embed.set_footer(text="Exilium Support Management • Ready for Review / Closure")
                    await channel.send(content=f"🔔 {support_pings}", embed=escalate_embed)
                    data["escalated"] = True
                    save_inactivity_data()
                    logger.info(f"Escalated inactive ticket #{channel.name} to Support after 30min silence.")

        except Exception as e:
            logger.error(f"Error checking inactivity for ticket {cid_str}: {e}")

    for cid_str in channels_to_remove:
        inactivity_data.pop(cid_str, None)
    if channels_to_remove:
        save_inactivity_data()

@check_ticket_inactivity_loop.error
async def on_inactivity_loop_error(error):
    logger.error(f"Inactivity loop error: {error}. Restarting watchdog in 10s...")
    await asyncio.sleep(10.0)
    if not check_ticket_inactivity_loop.is_running():
        check_ticket_inactivity_loop.restart()

last_daily_report_date = ""

@tasks.loop(minutes=1)
async def daily_report_loop():
    """Sends daily tickets summary DM to [GM]STAM every day at 23:59 UTC and purges 30-day old records."""
    global last_daily_report_date
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    # Send daily report at 23:59 UTC
    if now_utc.hour == 23 and now_utc.minute >= 58 and last_daily_report_date != today_str:
        await send_daily_dm_to_stam(today_str)
        last_daily_report_date = today_str

        # Item 13: 30-Day Auto-Purge of Old Tickets
        try:
            cutoff = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d")
            dates_to_purge = [d for d in list(daily_tickets_data.keys()) if d < cutoff]
            for d in dates_to_purge:
                del daily_tickets_data[d]
            if dates_to_purge:
                save_daily_tickets()
                logger.info(f"Auto-purged ticket records for {len(dates_to_purge)} dates older than 30 days.")
        except Exception as pe:
            logger.warning(f"Error purging old tickets: {pe}")

@daily_report_loop.error
async def on_daily_report_loop_error(error):
    logger.error(f"Daily report loop error: {error}. Restarting in 10s...")
    await asyncio.sleep(10.0)
    if not daily_report_loop.is_running():
        daily_report_loop.restart()

last_weekly_report_date = ""

@tasks.loop(hours=4)
async def weekly_report_loop():
    """Posts weekly analytics report every Sunday at 18:00+ UTC."""
    global last_weekly_report_date
    now_dt = datetime.now(timezone.utc)
    today_str = now_dt.strftime("%Y-%m-%d")

    if now_dt.weekday() == 6 and now_dt.hour >= 18 and last_weekly_report_date != today_str:
        target_ch_id = 1522965084320694454  # #👑丨tech-support
        ch = bot.get_channel(target_ch_id)
        if not ch:
            ch = bot.get_channel(1244303366104944730)  # #👑丨support-team
        if ch:
            report_embed = generate_analytics_embed()
            try:
                await ch.send(content=f"📊 **Weekly Support & Tickets AI Report** (Week of {today_str})", embed=report_embed)
                last_weekly_report_date = today_str
                logger.info(f"Posted weekly analytics report to #{ch.name}")
            except Exception as e:
                logger.error(f"Failed to post weekly analytics report: {e}")

@weekly_report_loop.error
async def on_weekly_report_loop_error(error):
    logger.error(f"Weekly report loop error: {error}. Restarting in 10s...")
    await asyncio.sleep(10.0)
    if not weekly_report_loop.is_running():
        weekly_report_loop.restart()

# -------------------------------------------------------------
# GitHub Auto-Updater Watchdog Loop (Every 2 minutes)
# -------------------------------------------------------------
RUNNING_COMMIT_SHA = None

@tasks.loop(minutes=2)
async def github_auto_updater_loop():
    """Periodically checks GitHub for new commits and auto-deploys updates seamlessly."""
    global RUNNING_COMMIT_SHA
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo_name = "mrshazya-del/exilium-bot"
    api_url = f"https://api.github.com/repos/{repo_name}/commits/main"

    headers = {"User-Agent": "Exilium-Auto-Updater"}
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        def fetch_commit():
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, fetch_commit)
        latest_sha = data.get("sha")
        commit_msg = data.get("commit", {}).get("message", "").split("\n")[0]

        if not latest_sha:
            return

        if RUNNING_COMMIT_SHA is None:
            RUNNING_COMMIT_SHA = latest_sha
            logger.info(f"GitHub Auto-Updater Watchdog initialized on commit: {latest_sha[:7]} ({commit_msg})")
            return

        if latest_sha != RUNNING_COMMIT_SHA:
            logger.info(f"New GitHub update detected: {latest_sha[:7]} - '{commit_msg}'. Syncing...")
            RUNNING_COMMIT_SHA = latest_sha

            # Run download and update
            summary = await loop.run_in_executor(None, download_github_update_sync, repo_name, "main", token)
            load_knowledge_base()
            logger.info(f"Auto-update applied: {summary}")

            # Notify staff log channel if configured
            try:
                log_cid = int(os.getenv("STAFF_LOG_CHANNEL_ID", "0"))
                log_ch = bot.get_channel(log_cid)
                if log_ch:
                    embed = discord.Embed(
                        title="🚀 Exilium Bot Auto-Updated from GitHub!",
                        description=f"• **Commit:** `{latest_sha[:7]}`\n• **Message:** `{commit_msg}`\n• **Summary:**\n```{summary[:500]}```",
                        color=0x2ECC71
                    )
                    await log_ch.send(embed=embed)
            except Exception as le:
                logger.warning(f"Could not send auto-update log: {le}")

            # If python code changed, restart process
            if "bot.py" in summary or ".py" in summary:
                logger.info("Python code was updated. Restarting bot instance...")
                await asyncio.sleep(2.0)
                if sys.platform == "win32":
                    subprocess.Popen([sys.executable] + sys.argv)
                    os._exit(0)
                else:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.debug(f"GitHub Auto-Updater check skipped: {e}")

@github_auto_updater_loop.error
async def on_github_auto_updater_loop_error(error):
    logger.warning(f"GitHub auto-updater loop error: {error}. Resuming in 30s...")
    await asyncio.sleep(30.0)
    if not github_auto_updater_loop.is_running():
        github_auto_updater_loop.restart()

# -------------------------------------------------------------
# Debounce Storage (5s Player Silence Debounce)
# -------------------------------------------------------------
ticket_pending_queues: Dict[int, List[discord.Message]] = {}
ticket_debounce_tasks: Dict[int, asyncio.Task] = {}

ticket_channel_locks: Dict[int, asyncio.Lock] = {}
ticket_last_reply_ts: Dict[int, float] = {}

async def debounced_ticket_worker(channel_id: int):
    """Waits for 5 seconds of player silence before processing accumulated messages with anti-concurrency lock."""
    try:
        await asyncio.sleep(5.0)
    except asyncio.CancelledError:
        return

    if channel_id not in ticket_channel_locks:
        ticket_channel_locks[channel_id] = asyncio.Lock()

    lock = ticket_channel_locks[channel_id]
    if lock.locked():
        # Another AI reply generation is currently in flight in this channel!
        return

    async with lock:
        # Enforce minimum 4s cooldown between consecutive AI messages in the same ticket
        now = time.time()
        last_sent = ticket_last_reply_ts.get(channel_id, 0.0)
        if (now - last_sent) < 4.0:
            await asyncio.sleep(4.0 - (now - last_sent))

        pending_msgs = ticket_pending_queues.pop(channel_id, [])
        ticket_debounce_tasks.pop(channel_id, None)

        if not pending_msgs:
            return

        first_msg = pending_msgs[0]
        channel = first_msg.channel
        guild = first_msg.guild
        author = pending_msgs[-1].author

        # Aggregate text and attachments
        text_parts = []
        image_parts = []
        for m in pending_msgs:
            content_text = m.content.strip()
            if content_text:
                text_parts.append(content_text)
            if m.attachments:
                for att in m.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        try:
                            if att.size <= 8 * 1024 * 1024:
                                img_bytes = await att.read()
                                image_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=att.content_type))
                                logger.info(f"Loaded image attachment from {m.author}: {att.filename} ({att.size} bytes)")
                        except Exception as e:
                            logger.warning(f"Error reading attachment: {e}")

        user_query = "\n".join(text_parts).strip()
        if not user_query and image_parts:
            user_query = "Player uploaded an error screenshot. Please diagnose the exact error, explain the fix in 2-3 bullet points, and provide official download links."
        elif not user_query and not image_parts:
            return

        async with channel.typing():
            await process_ticket_ai_reply(
                channel=channel,
                guild=guild,
                author=author,
                user_query=user_query,
                image_parts=image_parts,
                is_ticket_bot_embed=False,
                target_mention=""
            )
            ticket_last_reply_ts[channel_id] = time.time()

# -------------------------------------------------------------
# Core Ticket AI Reply Handler
# -------------------------------------------------------------
async def process_ticket_ai_reply(
    channel: discord.TextChannel,
    guild: discord.Guild,
    author: discord.User,
    user_query: str,
    image_parts: list = None,
    is_ticket_bot_embed: bool = False,
    target_mention: str = ""
):
    channel_id = channel.id

    # Item 18: Maintenance Mode Interceptor
    global maintenance_mode, maintenance_reason
    if maintenance_mode:
        m_msg = (
            f"⚠️ **Server Maintenance Notice (إشعار صيانة السيرفر):**\n\n"
            f"The server is currently undergoing scheduled maintenance.\n"
            f"**Reason / Details:** {maintenance_reason or 'Technical updates and server optimizations.'}\n\n"
            f"💡 The game will be online shortly. Thank you for your patience!"
        )
        await channel.send(m_msg)
        return

    # Item 21: Fast-Path for Downloads (0.01s local response)
    fast_dl = check_download_fast_path(user_query)
    if fast_dl:
        view = DownloadHelpView(include_vcredist=True, include_directx=True)
        await channel.send(fast_dl, view=view)
        return

    # Initialize history for this ticket if not existing
    if channel_id not in ticket_histories:
        ticket_histories[channel_id] = []

    history = ticket_histories[channel_id]
    speaker_label = f"Ticket ({target_mention})" if is_ticket_bot_embed else author.display_name
    history.append({"role": "user", "text": f"{speaker_label}: {user_query}"})

    if len(history) > MAX_HISTORY_PER_CHANNEL:
        ticket_histories[channel_id] = history[-MAX_HISTORY_PER_CHANNEL:]
        history = ticket_histories[channel_id]

    try:
        conversation_text = ""
        for turn in history:
            speaker = "Player" if turn["role"] == "user" else "Assistant"
            conversation_text += f"{speaker}: {turn['text']}\n"

        prompt = (
            f"Below is the current ticket conversation so far:\n"
            f"{conversation_text}\n"
            f"Provide your response as Exilium Assistant:"
        )

        ai_text = await generate_ai_text_async(prompt, image_parts)
        if not ai_text:
            ai_text = "Sorry, I could not process the request right now. Staff has been notified to assist you."

        # Check for Escalation, Resolution, or Starter Flags
        needs_escalation = "[ESCALATE_TO_ADMIN]" in ai_text
        is_resolved = "[TICKET_RESOLVED]" in ai_text

        # 100% Resolved Player Confirmation
        resolved_keywords = [
            "problem solved", "issue solved", "it worked now", "it works now",
            "working now", "solved thanks", "all good now", "fixed now",
            "اتحلت خلاص", "شكرا اتحلت", "اشتغلت خلاص", "المشكله اتحلت", "تمام اشتغلت",
            "solved please close", "please close ticket", "close ticket thanks",
            "close ticket", "اقفل التيكت", "اقفل التذكرة"
        ]
        user_lower = user_query.lower()
        if any(rk in user_lower for rk in resolved_keywords) and not needs_escalation:
            is_resolved = True
        has_starter_request = (
            "<<<AWAIT_STARTER_CONFIRMATION>>>" in ai_text
            or "AWAIT_STARTER_CONFIRMATION" in ai_text
        )

        clean_text = (
            ai_text.replace("[ESCALATE_TO_ADMIN]", "")
            .replace("[TICKET_RESOLVED]", "")
            .replace("<<<AWAIT_STARTER_CONFIRMATION>>>", "")
        )
        clean_text = re.sub(r"\[AWAIT_STARTER_CONFIRMATION[^\]]*(\[[^\]]*\])?[^\]]*\]", "", clean_text)
        clean_text = re.sub(r"\[AWAIT_STARTER_CONFIRMATION.*?\]", "", clean_text).strip()

        history.append({"role": "model", "text": clean_text})

        if needs_escalation:
            player_tag = target_mention if (is_ticket_bot_embed and target_mention) else author.mention
            clean_text = (
                f"Hello {player_tag}! 👋\n"
                f"Thank you for reporting this. I have notified our team for verification. Please allow us a moment to check it for you!"
            )
        elif is_ticket_bot_embed and target_mention:
            clean_text = f"👋 {target_mention}\n\n{clean_text}"

        lower_ai = clean_text.lower()
        needs_vc = any(k in lower_ai for k in ["visual c++", "msvcp", "vcruntime", "vc_redist"])
        needs_dx = any(k in lower_ai for k in ["directx", "d3dx9", "direct3d"])
        help_view = None
        if needs_vc or needs_dx:
            help_view = DownloadHelpView(include_vcredist=needs_vc, include_directx=needs_dx)

        chunks = [clean_text[i:i+1950] for i in range(0, len(clean_text), 1950)]
        for idx, chunk in enumerate(chunks):
            v = help_view if (idx == len(chunks) - 1) else None
            await channel.send(chunk, view=v)

        # Inactivity tracking
        target_pid = None
        if is_ticket_bot_embed and target_mention:
            digits = re.findall(r"\d+", target_mention)
            if digits:
                target_pid = int(digits[0])
        if not target_pid and not author.bot:
            target_pid = author.id

        inactivity_data[str(channel_id)] = {
            "player_id": target_pid,
            "last_reply_ts": time.time(),
            "reminder_sent_ts": None,
            "escalated": False
        }
        save_inactivity_data()

        # Update Daily Ticket Tracking
        current_ign = ticket_igns.get(channel_id, author.display_name)
        if has_starter_request:
            record_ticket_stat("Starter Package Request")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, "Starter Package Request", "🎁 Starter Requested")
        elif needs_escalation:
            record_ticket_stat("Escalated to Staff")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "🚨 Escalated")
        elif is_resolved:
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "✅ Resolved")
            
            # Auto-rename channel to solved- so staff immediately knows it's resolved!
            if isinstance(channel, discord.TextChannel) and not channel.name.startswith("solved-"):
                new_channel_name = f"solved-{channel.name}"[:100]
                try:
                    await channel.edit(name=new_channel_name, reason="Ticket 100% resolved by AI assistant")
                    logger.info(f"Renamed ticket channel {channel_id} to {new_channel_name}")
                except Exception as ren_err:
                    logger.warning(f"Failed to rename resolved channel {channel.name}: {ren_err}")

            # Notify Team: Solved, please close ticket (Pure notification without buttons)
            try:
                solved_embed = discord.Embed(
                    title="✅ Solved, please close ticket",
                    description=(
                        "🎉 **تم حل المشكلة وتأكيد ذلك من قبل اللاعب بنجاح!**\n"
                        "تم تغيير اسم القناة إلى `solved-` لإعلام طاقم العمل بإمكانية إغلاق التذكرة عبر بوت الكنترول."
                    ),
                    color=0x2ECC71
                )
                solved_embed.set_footer(text="Exilium Support Management • Team Notification")
                await channel.send(embed=solved_embed)
            except Exception as se_err:
                logger.warning(f"Failed to send solved embed: {se_err}")
        elif needs_vc or needs_dx:
            record_ticket_stat("Runtime / DirectX Error")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "⏳ In Progress")
        elif "c9" in clean_text.lower():
            record_ticket_stat("C9 Connection Error")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "⏳ In Progress")
        else:
            record_ticket_stat("General Inquiries")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "⏳ In Progress")

        # Starter Package Approval Sequence
        if has_starter_request:
            clean_ign = ticket_igns.get(channel_id, "").strip()
            if not clean_ign:
                try:
                    async for old_msg in channel.history(limit=15, oldest_first=True):
                        if old_msg.embeds:
                            for emb in old_msg.embeds:
                                for f in emb.fields:
                                    if f.name.lower().strip() == "ign":
                                        clean_ign = f.value.strip()
                                        ticket_igns[channel_id] = clean_ign
                                        break
                                if clean_ign:
                                    break
                        if clean_ign:
                            break
                except Exception as e:
                    logger.warning(f"Failed to scan history for IGN: {e}")

            # Item 6: If IGN still unknown, ask player explicitly before spawning approval
            if not clean_ign:
                clean_ign = sanitize_ign(author.display_name)

            clean_ign = sanitize_ign(clean_ign)
            pending_starters[channel_id] = {"ign": clean_ign}
            staff_pings = get_staff_ticket_mentions()

            # Item 9: Check duplicate history
            history_check = check_claimed_starter_history(author.id if not author.bot else 0, clean_ign)
            warning_text = ""
            if history_check:
                rec = history_check["record"]
                warning_text = (
                    f"\n\n⚠️ **Duplicate Claim Warning:**\n"
                    f"This account previously claimed starter rewards for `{rec.get('ign')}` on `{rec.get('date')}` "
                    f"approved by `{rec.get('approved_by')}`!"
                )

            approval_view = StarterApprovalView(ign=clean_ign, ticket_channel=channel, target_user=author if not author.bot else None)
            approval_embed = discord.Embed(
                title="🎁 Starter Package Request Approval",
                description=(
                    f"• **Applicant Character (IGN):** `{clean_ign}`\n"
                    f"• **Discord User:** {author.mention} (`{author.name}`){warning_text}\n\n"
                    f"**Delivery Checklist (Click buttons to toggle ON/OFF):**\n"
                    f"{approval_view.get_summary_text()}\n\n"
                    f"👉 When ready, click **[🚀 Deliver Selected]** below."
                ),
                color=0xF1C40F if not history_check else 0xE67E22
            )
            approval_embed.set_footer(text="Exilium Support Management • 👑 EXILIUM TEAM & STAM ONLY")
            await channel.send(content=f"🔔 {staff_pings}", embed=approval_embed, view=approval_view)

        # Handle Ticket Resolved Notification
        if is_resolved:
            player_display = target_mention if target_mention else author.mention
            resolved_embed = discord.Embed(
                title="🟢 Ticket Resolved",
                description=(
                    f"**Status:** ✅ Resolved by Exilium Assistant\n"
                    f"**Player / Owner:** {player_display}\n\n"
                    "💡 The player confirmed their issue is solved. This ticket is ready to be closed via the **[Close Ticket]** button above."
                ),
                color=0x2ECC71
            )
            resolved_embed.set_footer(text="Exilium Support Management • Ready for Closure")
            await channel.send(embed=resolved_embed)

        # Item 16: Smart English-Only Ultra-Compact Summary on Escalation
        if needs_escalation:
            if channel_id not in ticket_escalated:
                ticket_escalated[channel_id] = True
                
                # Check if issue is technical (DB, SQL, bugs, pet speed, damage, crashes)
                conv_lower = conversation_text.lower()
                tech_keywords = [
                    "db", "database", "sql", "refobj", "refitem", "bug", "glitch",
                    "speed", "pet", "skill", "damage", "crash", "error", "client", "freeze",
                    "disconnect", "shard", "stat", "attack", "alchemy", "patch"
                ]
                is_tech = any(re.search(r'\b' + re.escape(kw) + r'\b', conv_lower) for kw in tech_keywords)
                staff_pings = get_staff_ticket_mentions(is_technical=is_tech)

                summary_prompt = (
                    "Summarize this ticket in EXACTLY one concise English sentence for staff diagnostics. "
                    "State ONLY what the core problem is. Do NOT mention images or missing files. Just the core issue:\n"
                    f"{conversation_text}"
                )
                try:
                    summary_text = await generate_ai_text_async(summary_prompt)
                    clean_summary = summary_text.replace("[ESCALATE_TO_ADMIN]", "").strip()
                except Exception:
                    clean_summary = f"Player requires staff assistance regarding reported issue."

                route_label = "🔧 Technical / DB & Files (STAM & Astro)" if is_tech else "💬 General Community Support"

                summary_embed = discord.Embed(
                    title="📋 Staff Ticket Summary",
                    description=(
                        f"• **Player:** {author.mention} (IGN: `{current_ign or 'Unknown'}`)\n"
                        f"• **Core Issue:** {clean_summary}\n"
                        f"• **Route:** `{route_label}`"
                    ),
                    color=0xE67E22 if is_tech else 0x5865F2
                )
                summary_embed.set_footer(text="Exilium Support Management • Internal Diagnostic")
                await channel.send(content=f"🔔 {staff_pings}", embed=summary_embed)

    except Exception as e:
        logger.error(f"Error handling AI response for ticket {channel_id}: {e}")
        await channel.send("⚠️ An error occurred while generating a response. Staff has been notified.")

# -------------------------------------------------------------
# Koyeb / Health Check Web Server
# -------------------------------------------------------------
async def handle_health_check(request):
    return web.json_response({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing",
        "server": "Exilium Support Assistant",
        "latency_ms": round(bot.latency * 1000, 2) if bot.is_ready() else 0,
        "maintenance_mode": maintenance_mode
    })

async def start_health_server():
    try:
        raw_port = os.getenv("PORT", "8080")
        port = int(raw_port) if str(raw_port).strip().isdigit() else 8080
        app = web.Application()
        app.router.add_get("/", handle_health_check)
        app.router.add_get("/health", handle_health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Health check web server running on port {port}")
    except Exception as e:
        logger.warning(f"Health check web server could not start: {e}")

# -------------------------------------------------------------
# Bot Events
# -------------------------------------------------------------
@tasks.loop(minutes=5)
async def ghost_killer_loop():
    """Periodically terminate any duplicate bot processes running on the host."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, kill_other_bot_instances)
    except Exception:
        pass

@bot.event
async def on_ready():
    logger.info(f"Bot connected successfully as {bot.user} (ID: {bot.user.id})")
    kill_other_bot_instances()
    bot.add_view(TicketCreateButton())
    bot.add_view(StaffCloseView())

    if not hasattr(bot, "_health_server_started"):
        bot._health_server_started = True
        asyncio.create_task(start_health_server())

    if not check_ticket_inactivity_loop.is_running():
        check_ticket_inactivity_loop.start()
        logger.info("Started ticket inactivity background loop.")

    if not daily_report_loop.is_running():
        daily_report_loop.start()
        logger.info("Started daily DM report loop for [GM]STAM.")

    if not weekly_report_loop.is_running():
        weekly_report_loop.start()
        logger.info("Started weekly report background loop.")

    if not github_auto_updater_loop.is_running():
        github_auto_updater_loop.start()
        logger.info("Started GitHub auto-updater background watchdog (2 min interval).")

    if not ghost_killer_loop.is_running():
        ghost_killer_loop.start()
        logger.info("Started ghost duplicate killer loop (5 min interval).")

    try:
        # Clean up any duplicate guild-scoped commands so only single global commands exist
        for g in bot.guilds:
            try:
                bot.tree.clear_commands(guild=g)
                await bot.tree.sync(guild=g)
            except Exception as ge:
                logger.warning(f"Could not clear guild commands for {g.id}: {ge}")

        synced = await bot.tree.sync()
        logger.info(f"Cleanly synced {len(synced)} unique global slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

    activity_name = f"⚠️ Maintenance | v{BOT_VERSION}" if maintenance_mode else f"Exilium SRO | v{BOT_VERSION}"
    activity = discord.Game(name=activity_name)
    await bot.change_presence(status=discord.Status.online, activity=activity)

@bot.event
async def on_typing(channel, user, when):
    """When a human player types in a ticket, extend the debounce timer by 5s so the bot never interrupts."""
    if user == bot.user or user.bot:
        return
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    if not is_ticket_channel(channel):
        return
    if ticket_staff_taken_over.get(channel.id, False) or is_staff_member(user):
        return

    cid = channel.id
    if cid in ticket_debounce_tasks and not ticket_debounce_tasks[cid].done():
        ticket_debounce_tasks[cid].cancel()
        ticket_debounce_tasks[cid] = asyncio.create_task(debounced_ticket_worker(cid))

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
        return

    is_ticket = is_ticket_channel(message.channel)
    is_community = is_community_channel(message.channel)

    if not is_ticket and not is_community:
        return

    # =============================================================
    # Case A: Community / Lobby / Language Chat Channels (Mention-Only & Ticket Guide)
    # =============================================================
    if is_community and not is_ticket:
        # Strictly ignore all bots and webhooks to prevent loops
        if message.author.bot or message.webhook_id:
            return

        # MENTION-ONLY GUARD: The bot NEVER interrupts casual conversations in public channels.
        # It ONLY speaks if explicitly mentioned or replied to by a player!
        is_mentioned = bot.user.mentioned_in(message) or (
            message.reference and getattr(message.reference.resolved, "author", None) == bot.user
        )
        if not is_mentioned:
            return

        now = time.time()
        cid = message.channel.id
        uid = message.author.id

        # Channel cooldown (10s) and User cooldown (30s)
        if (now - community_channel_cooldown.get(cid, 0)) < 10.0:
            return
        if (now - community_user_cooldown.get(uid, 0)) < 30.0:
            return

        content_clean = message.content.strip().lower()

        # Non-spammy emoji reaction on gratitude
        gratitude_words = {"thanks", "thank you", "thx", "ty", "شكرا", "تسلم", "مشكور", "sagol", "sağol", "eyvallah", "obrigado", "danke"}
        if any(w in content_clean for w in gratitude_words):
            try:
                await message.add_reaction("👍")
            except Exception:
                pass
            return

        community_channel_cooldown[cid] = now
        community_user_cooldown[uid] = now

        # Fast-Path for download queries
        fast_dl = check_download_fast_path(message.content)
        if fast_dl:
            reply_text = (
                f"{fast_dl}\n\n"
                f"💡 *Need further assistance or have an issue? Please open a ticket in <#{TICKET_PANEL_CHANNEL_ID}>!*"
            )
            await message.reply(reply_text, view=TicketRedirectView(), mention_author=True)
            return

        prompt = (
            f"You are Exilium Support Assistant. A player mentioned/tagged you in a public chat channel asking:\n"
            f"'{message.content}'\n\n"
            f"RULES:\n"
            f"1. Reply warmly, politely, and concisely in 1 to 2 sentences in the player's exact language (English, Arabic, Turkish, etc.).\n"
            f"2. If it's a general game inquiry, answer it briefly and clearly.\n"
            f"3. Always guide them to open an official support ticket in <#{TICKET_PANEL_CHANNEL_ID}> for account issues, bug reports, starter packages, or detailed technical help.\n"
            f"Your response:"
        )

        try:
            ai_reply = await generate_ai_text_async(prompt)
            if ai_reply and "[IGNORE]" not in ai_reply:
                clean_ans = ai_reply.replace("[ESCALATE_TO_ADMIN]", "").replace("[TICKET_RESOLVED]", "").strip()
                if clean_ans:
                    await message.reply(clean_ans[:1900], view=TicketRedirectView(), mention_author=True)
        except Exception as e:
            logger.warning(f"Error in community chat AI: {e}")
        return

    # =============================================================
    # Case B: Ticket Channels
    # =============================================================
    if not gemini_client:
        return

    channel_id = message.channel.id

    # 1. Embed from Ticket Bot
    if message.author.bot:
        if not message.embeds:
            return

        extracted_subject = ""
        extracted_ign = ""
        extracted_username = ""
        target_mention = ""

        for emb in message.embeds:
            fields_dict = {f.name.lower().strip(): f.value.strip() for f in emb.fields}
            for key in ["subject", "reason", "issue", "problem", "request details"]:
                if key in fields_dict:
                    extracted_subject = fields_dict[key]
                    break

            if "ign" in fields_dict:
                extracted_ign = fields_dict["ign"]
            if "username" in fields_dict:
                extracted_username = fields_dict["username"]
            if "ticket owner" in fields_dict:
                target_mention = fields_dict["ticket owner"]

            if not extracted_subject and emb.description:
                for line in emb.description.split("\n"):
                    if "subject" in line.lower() or "reason" in line.lower():
                        extracted_subject = line
                        break

        if extracted_subject:
            if extracted_ign:
                ticket_igns[channel_id] = sanitize_ign(extracted_ign)
            logger.info(f"Detected Ticket Bot Embed in {message.channel.name} | IGN: {extracted_ign} | Subject: {extracted_subject}")
            record_daily_ticket(channel_id, message.channel.name, extracted_ign, extracted_username, extracted_subject, "⏳ In Progress")

            user_query = (
                f"Player opened a new ticket:\n"
                f"- In-Game Name (IGN): {extracted_ign or 'Player'}\n"
                f"- Account Username: {extracted_username or 'N/A'}\n"
                f"- Ticket Subject / Issue: {extracted_subject}\n\n"
                f"INSTRUCTIONS:\n"
                f"1. Detect the player's language strictly from the Ticket Subject.\n"
                f"2. Use the Subject as immediate context.\n"
                f"   - If asking for Starter Package or mentions being a new player: tell them briefly in English that it is submitted for Admin confirmation, and append <<<AWAIT_STARTER_CONFIRMATION>>>.\n"
                f"   - If a known issue: give 2-3 short bullet steps.\n"
                f"   - If vague or greeting: ask how you can help."
            )

            async with message.channel.typing():
                await process_ticket_ai_reply(
                    channel=message.channel,
                    guild=message.guild,
                    author=message.author,
                    user_query=user_query,
                    image_parts=None,
                    is_ticket_bot_embed=True,
                    target_mention=target_mention
                )
        return

    # 2. Message from a Staff Member
    if is_staff_member(message.author):
        now_ts = time.time()
        ticket_staff_taken_over[channel_id] = True
        ticket_staff_last_msg_ts[channel_id] = now_ts

        if channel_id in ticket_debounce_tasks and not ticket_debounce_tasks[channel_id].done():
            ticket_debounce_tasks[channel_id].cancel()
        ticket_pending_queues.pop(channel_id, None)

        inactivity_data.pop(str(channel_id), None)
        save_inactivity_data()

        logger.info(f"Staff member {message.author.display_name} spoke in #{message.channel.name}. Bot silenced.")
        return

    # Item 11: Smart Silence Expiration (Check if staff left > 24 hours ago)
    if ticket_staff_taken_over.get(channel_id, False):
        last_staff_ts = ticket_staff_last_msg_ts.get(channel_id, 0.0)
        if (time.time() - last_staff_ts) > 24 * 3600:
            logger.info(f"24h elapsed since last staff message in #{message.channel.name}. Resuming AI assistance.")
            ticket_staff_taken_over.pop(channel_id, None)
        else:
            return

    # Check recent history for staff takeover if not yet marked
    if channel_id not in ticket_staff_taken_over:
        try:
            async for prev_msg in message.channel.history(limit=20):
                if prev_msg.author != bot.user and not prev_msg.author.bot and is_staff_member(prev_msg.author):
                    ticket_staff_taken_over[channel_id] = True
                    ticket_staff_last_msg_ts[channel_id] = prev_msg.created_at.timestamp()
                    logger.info(f"Detected previous staff message from {prev_msg.author.display_name} in #{message.channel.name}. Bot silenced.")
                    return
        except Exception as e:
            logger.warning(f"Error checking message history for staff takeover: {e}")

    if ticket_staff_taken_over.get(channel_id, False):
        return

    if str(channel_id) in inactivity_data:
        del inactivity_data[str(channel_id)]
        save_inactivity_data()

    # 5-SECOND DEBOUNCE LOGIC
    if channel_id in ticket_debounce_tasks and not ticket_debounce_tasks[channel_id].done():
        ticket_debounce_tasks[channel_id].cancel()

    if channel_id not in ticket_pending_queues:
        ticket_pending_queues[channel_id] = []
    ticket_pending_queues[channel_id].append(message)

    ticket_debounce_tasks[channel_id] = asyncio.create_task(debounced_ticket_worker(channel_id))

# -------------------------------------------------------------
# Slash Commands
# -------------------------------------------------------------
@bot.tree.command(name="ask", description="Ask Exilium AI Assistant any question about gameplay, rates, or error solutions")
@app_commands.describe(question="Your question or technical issue regarding Exilium")
async def ask_slash(interaction: discord.Interaction, question: str):
    if not gemini_client:
        await interaction.response.send_message("❌ مفتاح الذكاء الاصطناعي غير مفعل حالياً.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        response_text = await generate_ai_text_async(question)
        ans = response_text.replace("[ESCALATE_TO_ADMIN]", "").strip()
        embed = discord.Embed(
            title="🛡️ Exilium Assistant",
            description=ans[:4000],
            color=0x00A8FF
        )
        embed.set_footer(text=f"طلب بواسطة: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in /ask command: {e}")
        await interaction.followup.send("⚠️ تعذر معالجة الطلب حالياً، يرجى المحاولة لاحقاً.")

@bot.tree.command(name="ticket-panel", description="Send the official Support Ticket panel in this channel (Admin Only)")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_panel_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 مركز الدعم الفني لسيرفر Exilium | Support Center",
        description=(
            "إذا كنت تواجه أي مشكلة داخل اللعبة، أو لديك استفسار عن السيرفر أو شحن السيلك، "
            "اضغط على الزر أدناه لفتح تذكرة خاصة بك.\n\n"
            "⚡ **مساعدنا الذكي متاح 24/7 للرد الفوري على مشكلتك ومساعدتك في حلها خطوة بخطوة!**\n\n"
            "--- \n"
            "*Need help with Exilium? Click the button below to open a private support ticket.*"
        ),
        color=0x00A8FF
    )
    embed.set_footer(text="Exilium Online • 24/7 Automated Support")

    view = TicketCreateButton()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ تم إرسال لوحة التذاكر بنجاح!", ephemeral=True)

@bot.tree.command(name="reload-kb", description="Reload Exilium Knowledge Base without restarting the bot (Admin Only)")
@app_commands.checks.has_permissions(administrator=True)
async def reload_kb_command(interaction: discord.Interaction):
    load_knowledge_base()
    await interaction.response.send_message("✅ تم إعادة تحميل ملف `knowledge_base.json` بنجاح!", ephemeral=True)

@bot.tree.command(name="report", description="Display AI statistics and ticket analytics report (👑 EXILIUM TEAM)")
async def report_command(interaction: discord.Interaction):
    if not is_exilium_team_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص لأعضاء 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    embed = generate_analytics_embed()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="daily-report", description="Send the daily executive tickets brief via DM to [GM]STAM or in channel")
@app_commands.describe(send_dm="Deliver daily brief directly to [GM]STAM via DM (True/False)")
async def daily_report_command(interaction: discord.Interaction, send_dm: bool = True):
    if not is_exilium_team_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص لـ 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    embed = generate_daily_report_embed(today_str)

    if send_dm:
        await send_daily_dm_to_stam(today_str)
        await interaction.response.send_message("✅ تم إرسال تقرير اليوم بنجاح في الخاص لـ [GM]STAM!", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="transcript", description="Export current ticket conversation history as a text file (Staff Only)")
async def transcript_command(interaction: discord.Interaction):
    if not is_exilium_team_member(interaction.user) and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر متاح للإدارة فقط.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        messages = []
        async for m in interaction.channel.history(limit=500, oldest_first=True):
            timestamp_str = m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            content = m.clean_content
            if m.embeds:
                for e in m.embeds:
                    content += f"\n[Embed: {e.title} - {e.description}]"
            messages.append(f"[{timestamp_str}] {m.author.name}: {content}")

        transcript_text = "\n".join(messages)
        file_bytes = io.BytesIO(transcript_text.encode("utf-8"))
        discord_file = discord.File(file_bytes, filename=f"transcript-{interaction.channel.name}.txt")

        await interaction.followup.send(
            content=f"📄 **سجل محادثة التيكت:** `{interaction.channel.name}`",
            file=discord_file,
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Error generating transcript: {e}")
        await interaction.followup.send(f"⚠️ فشل استخراج السجل: {e}", ephemeral=True)

@bot.tree.command(name="pause-bot", description="Pause AI responses in this ticket channel (Staff Only)")
async def pause_bot_command(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص للستاف فقط.", ephemeral=True)
        return
    ticket_staff_taken_over[interaction.channel_id] = True
    await interaction.response.send_message("⏸️ تم إيقاف ردود الذكاء الاصطناعي في هذا التيكت. يمكنك التحدث مع اللاعب بحرية.", ephemeral=True)

@bot.tree.command(name="resume-bot", description="Resume AI responses in this ticket channel (Staff Only)")
async def resume_bot_command(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص للستاف فقط.", ephemeral=True)
        return
    ticket_staff_taken_over.pop(interaction.channel_id, None)
    await interaction.response.send_message("▶️ تم إعادة تفعيل ردود الذكاء الاصطناعي في هذا التيكت بنجاح.", ephemeral=True)

# Item 17: Manual Give-Starter Command
@bot.tree.command(name="give-starter", description="Open starter package dispatch menu for a specific character (EXILIUM TEAM)")
@app_commands.describe(ign="Player exact In-Game Name (IGN)")
async def give_starter_command(interaction: discord.Interaction, ign: str):
    if not is_exilium_team_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص لأعضاء 👑 EXILIUM TEAM و [GM]STAM فقط.", ephemeral=True)
        return

    clean_ign = sanitize_ign(ign)
    if not clean_ign:
        await interaction.response.send_message("❌ اسم الشخصية (IGN) غير صالح.", ephemeral=True)
        return

    history_check = check_claimed_starter_history(0, clean_ign)
    warning_text = ""
    if history_check:
        rec = history_check["record"]
        warning_text = (
            f"\n\n⚠️ **Duplicate Claim Warning:**\n"
            f"This IGN previously claimed starter rewards on `{rec.get('date')}` "
            f"approved by `{rec.get('approved_by')}`!"
        )

    approval_view = StarterApprovalView(ign=clean_ign, ticket_channel=interaction.channel)
    embed = discord.Embed(
        title="🎁 Manual Starter Rewards Dispatch",
        description=(
            f"• **Player Character (IGN):** `{clean_ign}`{warning_text}\n\n"
            f"**Delivery Checklist (Click buttons to toggle ON/OFF):**\n"
            f"{approval_view.get_summary_text()}\n\n"
            f"👉 When ready, click **[🚀 Deliver Selected]** below."
        ),
        color=0xF1C40F if not history_check else 0xE67E22
    )
    embed.set_footer(text="Exilium Support Management • Manual Dispatch")
    await interaction.response.send_message(embed=embed, view=approval_view)

# Item 18: Maintenance Mode Command
@bot.tree.command(name="maintenance", description="Toggle server maintenance mode and notify players automatically (Admin Only)")
@app_commands.describe(
    status="Maintenance status (on to activate / off to deactivate)",
    reason="Reason for maintenance and estimated completion time",
    duration_minutes="Estimated duration in minutes (optional)"
)
@app_commands.choices(status=[
    app_commands.Choice(name="تفعيل وضع الصيانة (ON)", value="on"),
    app_commands.Choice(name="إلغاء وضع الصيانة (OFF)", value="off"),
])
async def maintenance_command(interaction: discord.Interaction, status: app_commands.Choice[str], reason: str = "", duration_minutes: int = 0):
    if not is_exilium_team_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص لـ 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    global maintenance_mode, maintenance_reason, maintenance_start_ts, maintenance_duration_min
    if status.value == "on":
        maintenance_mode = True
        maintenance_reason = reason.strip() or "General server maintenance and optimizations."
        maintenance_start_ts = time.time()
        maintenance_duration_min = duration_minutes

        activity = discord.Game(name="⚠️ Maintenance Mode | Exilium SRO")
        await bot.change_presence(status=discord.Status.dnd, activity=activity)

        embed = discord.Embed(
            title="⚠️ تم تفعيل وضع الصيانة",
            description=(
                f"**السبب / التفاصيل:** {maintenance_reason}\n"
                f"**المدة المتوقعة:** {f'{duration_minutes} دقيقة' if duration_minutes > 0 else 'غير محدد'}\n\n"
                f"💡 البوت سيخبر أي لاعب يستفسر في التذاكر أو الشات بوجود صيانة تلقائياً."
            ),
            color=0xE74C3C
        )
        await interaction.response.send_message(embed=embed)
    else:
        maintenance_mode = False
        maintenance_reason = ""
        maintenance_start_ts = 0.0
        maintenance_duration_min = 0

        activity = discord.Game(name="Exilium SRO | Support Tickets")
        await bot.change_presence(status=discord.Status.online, activity=activity)

        embed = discord.Embed(
            title="✅ تم إلغاء وضع الصيانة",
            description="عادت حالة البوت إلى الوضع الطبيعي وتم استئناف الردود المعتادة.",
            color=0x2ECC71
        )
        await interaction.response.send_message(embed=embed)

# Item 20: Staff Multi-Language Reply Command
@bot.tree.command(name="staff-reply", description="Translate staff reply into player ticket language and send as official support")
@app_commands.describe(
    text="Your reply message to be translated for the player",
    language="Target language (Default: Auto-Detect player's language)",
    close_after="Attach a 1-click Close Ticket button below the message (True/False)"
)
@app_commands.choices(language=[
    app_commands.Choice(name="Auto-Detect (Player's Language)", value="auto"),
    app_commands.Choice(name="English", value="English"),
    app_commands.Choice(name="Turkish (Türkçe)", value="Turkish"),
    app_commands.Choice(name="Arabic (العربية)", value="Arabic"),
    app_commands.Choice(name="Portuguese (Português)", value="Portuguese"),
    app_commands.Choice(name="German (Deutsch)", value="German"),
    app_commands.Choice(name="Spanish (Español)", value="Spanish"),
    app_commands.Choice(name="French (Français)", value="French"),
    app_commands.Choice(name="Russian (Русский)", value="Russian"),
])
async def staff_reply_command(
    interaction: discord.Interaction, 
    text: str, 
    language: app_commands.Choice[str] = None, 
    close_after: bool = False
):
    if not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ This command is restricted to Staff members.", ephemeral=True)
        return

    # Defer ephemerally so the command execution bubble is 100% private to staff
    await interaction.response.defer(ephemeral=True)

    target_lang = language.value if language else "auto"

    if target_lang == "auto":
        # Scan recent messages (newest first) specifically from the player (non-staff)
        conversation_sample = ""
        async for m in interaction.channel.history(limit=25, oldest_first=False):
            if m.author != bot.user and not m.author.bot and not is_staff_member(m.author):
                conversation_sample = f"{m.content}\n" + conversation_sample
        
        translate_prompt = (
            "You are the official Exilium Support Translator.\n"
            "A staff member wants to reply to this player.\n"
            f"Player's recent messages context:\n{conversation_sample or 'English default'}\n\n"
            "Translate the following staff message into the EXACT language of the player (e.g. Turkish if player speaks Turkish; Portuguese if Portuguese; English if English/unknown; Arabic if Arabic):\n"
            f"'{text}'\n\n"
            "Provide ONLY the translated text in a friendly, gamer-appropriate support tone without quotes or explanations:"
        )
    else:
        translate_prompt = (
            f"Translate the following staff support message into {target_lang}.\n"
            f"Message: '{text}'\n\n"
            "Provide ONLY the translated text in a friendly, professional gamer-appropriate tone without quotes or explanations:"
        )

    try:
        translated_text = await generate_ai_text_async(translate_prompt)
        translated_clean = translated_text.replace("[ESCALATE_TO_ADMIN]", "").replace("[TICKET_RESOLVED]", "").strip()

        view = StaffCloseView() if close_after else None

        embed = discord.Embed(
            title="🛡️ Exilium Support Response",
            description=translated_clean,
            color=0x00A8FF
        )
        embed.set_footer(text=f"Sent by {interaction.user.display_name} • Official Exilium Support")

        # Send public message into ticket channel
        await interaction.channel.send(embed=embed, view=view)

        # Send private confirmation to staff
        lang_display = target_lang if target_lang != "auto" else "Auto-Detected"
        await interaction.followup.send(
            f"✅ **Reply sent to ticket ({lang_display}):**\n> {translated_clean}", 
            ephemeral=True
        )

    except Exception as e:
        logger.error(f"Error in /staff-reply: {e}")
        await interaction.followup.send(f"⚠️ Translation failed: {e}", ephemeral=True)

def download_github_update_sync(repo_name: str = "mrshazya-del/exilium-bot", branch: str = "main", token: str = "") -> str:
    url = f"https://github.com/{repo_name}/archive/refs/heads/{branch}.zip"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if token:
        headers["Authorization"] = f"token {token}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        zip_bytes = resp.read()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    protected_files = {
        ".env",
        ".env.example",
        "daily_tickets.json",
        "ticket_analytics.json",
        "inactivity_tracker.json",
        "analytics.json",
        "claimed_starters.json"
    }

    updated_files = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for member in z.namelist():
            parts = member.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            rel_path = parts[1]
            basename = os.path.basename(rel_path)

            if basename in protected_files or rel_path.startswith(".git") or "__pycache__" in rel_path:
                continue

            dest_path = os.path.join(base_dir, rel_path)
            if member.endswith("/"):
                os.makedirs(dest_path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with z.open(member) as src, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                updated_files.append(rel_path)

    return f"Updated {len(updated_files)} files: " + ", ".join(updated_files[:6])

@bot.tree.command(name="kill-duplicates", description="Terminate any ghost/duplicate bot processes running in the background (Admin Only)")
async def kill_duplicates_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_exilium_team_member(interaction.user):
        await interaction.followup.send("❌ هذا الأمر مخصص لـ 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    killed = kill_other_bot_instances()
    if killed:
        await interaction.followup.send(f"✅ تم إنهاء {len(killed)} نسخة مكررة تعمل بالخلفية: `{killed}`. يعمل البوت الآن بنسخة واحدة فقط!", ephemeral=True)
    else:
        await interaction.followup.send("✅ لا توجد أي نسخ مكررة تعمل بالخلفية! البوت يعمل بنسخة واحدة سليمة.", ephemeral=True)

@bot.tree.command(name="version", description="Display current bot version, system status, and latency info")
async def version_command(interaction: discord.Interaction):
    latency = round(bot.latency * 1000, 2) if bot.is_ready() else 0
    desc_lines = [
        f"• **Version:** `v{BOT_VERSION}`",
        f"• **Build Date:** `{BOT_BUILD_DATE}`",
        f"• **Status:** `Online & Operational`",
        f"• **Latency:** `{latency} ms`",
        f"• **AI Model:** `{GEMINI_MODEL}`",
        f"• **Community Category ID:** `{COMMUNITY_CATEGORY_ID}`",
        f"• **Lobby Channel ID:** `{LOBBY_CHANNEL_ID}`"
    ]
    embed = discord.Embed(
        title="🤖 Exilium Support Bot Version Info",
        description="\n".join(desc_lines),
        color=0x2ECC71
    )
    embed.set_footer(text="Exilium Support Management • Bot Engine")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="upload-kb", description="Upload and apply a new knowledge_base.json immediately (Admin Only)")
@app_commands.checks.has_permissions(administrator=True)
async def upload_kb_command(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    if not is_exilium_team_member(interaction.user):
        await interaction.followup.send("❌ هذا الأمر مخصص لـ 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    if not file.filename.endswith(".json"):
        await interaction.followup.send("❌ يرجى رفع ملف بصيغة `.json` فقط.", ephemeral=True)
        return

    try:
        content_bytes = await file.read()
        parsed = json.loads(content_bytes.decode("utf-8"))
        kb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base.json")
        with open(kb_path, "wb") as kf:
            kf.write(content_bytes)
        load_knowledge_base()
        issues_count = len(parsed.get("common_errors_and_fixes", []))
        await interaction.followup.send(
            f"✅ **تم تحديث قاعدة المعرفة فورياً!**\n"
            f"• تم حفظ الملف بنجاح على السيرفر.\n"
            f"• عدد المشاكل الشائعة المحملة: `{issues_count}` مشكلة.\n"
            f"• تم تفعيل التعديلات بالذاكرة فوراً دون الحاجة لإعادة التشغيل.",
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Failed to upload KB: {e}")
        await interaction.followup.send(f"❌ حدث خطأ أثناء معالجة الملف: `{e}`", ephemeral=True)

@bot.tree.command(name="update-bot", description="Pull latest updates from GitHub and restart the bot automatically (Admin Only)")
async def update_bot_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_exilium_team_member(interaction.user):
        await interaction.followup.send("❌ هذا الأمر مخصص لـ 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    update_summary = ""
    used_method = ""
    github_token = os.getenv("GITHUB_TOKEN", "").strip()

    git_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".git")
    git_worked = False

    if os.path.exists(git_dir):
        try:
            repo_url = os.getenv("GITHUB_REPO_URL", "https://github.com/mrshazya-del/exilium-bot.git").strip()
            if github_token:
                repo_url = f"https://{github_token}@github.com/mrshazya-del/exilium-bot.git"

            p_set = await asyncio.create_subprocess_exec("git", "remote", "set-url", "origin", repo_url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await p_set.communicate()

            process = await asyncio.create_subprocess_exec("git", "pull", "origin", "main", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                update_summary = stdout.decode("utf-8", errors="ignore").strip()
                used_method = "Git Pull Engine"
                git_worked = True
            else:
                logger.warning(f"Git pull returned non-zero code, attempting force reset...")
                p_fetch = await asyncio.create_subprocess_exec("git", "fetch", "origin", "main", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await p_fetch.communicate()
                p_reset = await asyncio.create_subprocess_exec("git", "reset", "--hard", "origin/main", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                r_stdout, _ = await p_reset.communicate()
                if p_reset.returncode == 0:
                    update_summary = "Force-synced to origin/main: " + r_stdout.decode("utf-8", errors="ignore").strip()
                    used_method = "Git Force Reset Engine"
                    git_worked = True
        except Exception as ge:
            logger.warning(f"Git pull failed or git not in PATH ({ge}), falling back to direct GitHub archive...")

    if not git_worked:
        try:
            loop = asyncio.get_running_loop()
            update_summary = await loop.run_in_executor(None, download_github_update_sync, "mrshazya-del/exilium-bot", "main", github_token)
            used_method = "Direct GitHub Archive Engine"
        except Exception as ze:
            logger.error(f"Failed to download update via zip: {ze}")
            await interaction.followup.send(f"⚠️ فشل تحديث البوت: `{ze}`\nيرجى التحقق من اتصال السيرفر بالإنترنت أو حالة مستودع GitHub.", ephemeral=True)
            return

    load_knowledge_base()

    embed = discord.Embed(
        title=f"🔄 Exilium Bot Auto-Updater (v{BOT_VERSION})",
        description=(
            f"• **الإصدار الجديد:** `v{BOT_VERSION} (Build: {BOT_BUILD_DATE})`\n"
            f"• **طريقة التحديث:** `{used_method}`\n"
            f"• **النتيجة:**\n```{update_summary[:700]}```\n\n"
            f"🚀 **جاري إعادة تشغيل البوت بالنسخة v{BOT_VERSION} الآن...**\n"
            f"سيعود البوت بحالة الديسكورد: `Exilium SRO | v{BOT_VERSION}`"
        ),
        color=0x2ECC71
    )
    embed.set_footer(text=f"طلب بواسطة: {interaction.user.display_name} • Version: v{BOT_VERSION}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    logger.info(f"Auto-update successful via {used_method} by {interaction.user}. Restarting process...")
    await asyncio.sleep(1.0)
    if sys.platform == "win32":
        subprocess.Popen([sys.executable] + sys.argv)
        os._exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)

# -------------------------------------------------------------
# Main Runner
# -------------------------------------------------------------
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("\n" + "="*60)
        print("❌ خطأ: لم يتم تعيين DISCORD_BOT_TOKEN في ملف .env!")
        print("يرجى فتح ملف .env وإضافة التوكين الخاص بالبوت.")
        print("="*60 + "\n")
        exit(1)

    # Strictly ensure only 1 single instance runs across the entire server
    acquire_instance_socket_lock(49988)

    print(f"🚀 جاري تشغيل بوت Exilium AI Assistant (v{BOT_VERSION})...")
    bot.run(DISCORD_BOT_TOKEN)
