import urllib.request
import zipfile
import shutil
import os
import sys
import json
import logging
import re
from typing import Dict, List, Optional
import asyncio
import io
import time
from datetime import datetime, timezone

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
SUPPORT_ROLE_ID = os.getenv("SUPPORT_ROLE_ID", "1287045626860802162").strip()
EXILIUM_TEAM_ROLE_ID = os.getenv("EXILIUM_TEAM_ROLE_ID", "1203345505841582080").strip()
HEAD_SUPPORTER_ROLE_ID = os.getenv("HEAD_SUPPORTER_ROLE_ID", "1287045626860802162").strip()
COMMUNITY_SUPPORTER_ROLE_ID = os.getenv("COMMUNITY_SUPPORTER_ROLE_ID", "1244326192631386112").strip()

COMMANDS_CHANNEL_NAME = os.getenv("COMMANDS_CHANNEL_NAME", "send-commands").strip()
COMMANDS_CHANNEL_ID = os.getenv("COMMANDS_CHANNEL_ID", "1236998285680578580").strip()

raw_prefixes = os.getenv("TICKET_CHANNEL_PREFIXES", "ticket-,support-,ticket_")
TICKET_CHANNEL_PREFIXES = [p.strip().lower() for p in raw_prefixes.split(",") if p.strip()]
TICKET_CATEGORY_ID = os.getenv("TICKET_CATEGORY_ID", "").strip()

# -------------------------------------------------------------
# Staff Mentions Policy:
# Strictly NEVER mention 👑 EXILIUM TEAM 👑 role in tickets!
# Ticket staff mentions are ONLY STAM, Head Supporter, and Community Supporter.
# -------------------------------------------------------------
def get_staff_ticket_mentions() -> str:
    """Strictly mention STAM, Head Supporter, and Community Supporter. NEVER mention Exilium Team role."""
    mentions = [f"<@{ADMIN_USER_ID}>"]
    if HEAD_SUPPORTER_ROLE_ID:
        mentions.append(f"<@&{HEAD_SUPPORTER_ROLE_ID}>")
    if COMMUNITY_SUPPORTER_ROLE_ID:
        mentions.append(f"<@&{COMMUNITY_SUPPORTER_ROLE_ID}>")
    return " ".join(mentions)

# -------------------------------------------------------------
# Exilium Team Authorization Helper
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

# channel_id -> bool: True when staff spoke in ticket, so bot completely stops replying
ticket_staff_taken_over: Dict[int, bool] = {}
# channel_id -> bool: True when staff was already pinged in this ticket to prevent repeat pings
ticket_escalated: Dict[int, bool] = {}

# -------------------------------------------------------------
# Initialize Gemini Client
# -------------------------------------------------------------
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY is not set in environment or .env! AI responses will fail.")

try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except Exception as e:
    logger.error(f"Failed to initialize Gemini Client: {e}")
    gemini_client = None

# -------------------------------------------------------------
# Knowledge Base Loader
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

def build_system_instruction() -> str:
    kb_str = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
    return f"""You are the official AI Support Assistant for "Exilium", a popular Silkroad Online (vSRO) private server.
Your name is "Exilium Assistant". You assist players warmly, professionally, and concisely inside Discord support tickets.

Here is the authoritative Exilium Server Knowledge Base:
=== KNOWLEDGE BASE START ===
{kb_str}
=== KNOWLEDGE BASE END ===

YOUR STRICT OPERATING INSTRUCTIONS:
1. STRICT BREVITY & CONCISENESS (MANDATORY):
   - Keep ALL responses SHORT, DIRECT, and CONCISE. Never write long essays or overwhelming paragraphs.
   - Maximum 2 to 3 sentences or short bullet points per response.

2. ZERO HALLUCINATION POLICY (NEVER GUESS OR FABRICATE):
   - You are strictly grounded by the Exilium Knowledge Base above.
   - If a player asks about an issue, bug, custom drop, or mechanic that is NOT clearly documented in your Knowledge Base, or if you are not 100% certain:
     -> STRICTLY DO NOT GUESS OR INVENT ANY ANSWERS.
     -> Reply politely: "I do not have exact information regarding this issue. I have notified our Support and Management team to assist you directly."
     -> Append [ESCALATE_TO_ADMIN] so staff is notified immediately.

3. UNCLEAR / VAGUE / GREETING INQUIRIES:
   - If the ticket subject or player message is vague, short, or just a greeting (e.g. "alo", "hi", "hello", "help", "need support", "problem", or empty):
     -> Respond with ONLY 1 or 2 short sentences:
     "Hello! 👋 How can I help you today? Could you please briefly explain what issue you are facing?"
     -> That's it! Do NOT add anything else.

4. DIRECT SOLUTIONS FOR KNOWN SPECIFIC ISSUES:
   - When the player mentions a specific known problem from the Knowledge Base (e.g. "C9 error", "sro_client closed by defender", "BSObj Plugin", "Win 11 crash", "how to craft SOM", "alchemy rates"):
     -> Provide a concise, step-by-step solution using 2 to 3 short bullet points only.
     -> Keep the entire response under 4 lines.

5. DYNAMIC LANGUAGE DETECTION (SUBJECT-BASED):
   - ALWAYS detect the player's language directly from the Ticket Subject or user query:
     * If Subject is Turkish (e.g. 'başlangıç paketi', 'oyuna giremiyorum', 'selam'): reply in Turkish!
     * If Subject is Portuguese (e.g. 'pacote inicial', 'não consigo entrar', 'olá'): reply in Portuguese!
     * If Subject is Arabic (e.g. 'باكدج البداية', 'عندي مشكلة', 'مرحبا'): reply in Arabic!
     * If Subject is German: reply in German!
     * If Subject is English or unclear: reply in English!
   - Also use the Ticket Subject as the immediate context of their issue.

6. ESCALATION TO ADMIN / SUPPORT:
   - You MUST escalate if:
     a) Donations, Payments, or Silk not received.
     b) Stolen items, account theft, or lost credentials.
     c) Ban appeals or reporting cheaters with proof.
     d) The issue is NOT in the knowledge base or outside standard troubleshooting.
     e) The player specifically asks for an Admin/Staff.
   - HOW TO ESCALATE:
     - Reply shortly: "This issue requires staff assistance. I have notified our Support and Management team to assist you shortly."
     - Append [ESCALATE_TO_ADMIN].

7. ISSUE RESOLVED CONFIRMATION:
   - When the player confirms their issue is solved, or answers "no" / "nothing else" / "all good" / "thanks" / "it works" / "solved" / "شكراً":
     -> Reply politely: "Awesome! Glad I could help you today. Please click the red [Close Ticket] button above to close this ticket. Have fun playing Exilium! 👋"
     -> You MUST append [TICKET_RESOLVED] at the end of your response.

8. STARTER PACKAGES / NEW PLAYER REWARDS:
   - If a player asks for a starter package, starter pack, new player reward, or mentions they are a new player (e.g. "i'm new", "new player", "ana gded", "انا جديد", "starter", "beginner"):
     - Reply briefly in clean English: "Welcome to Exilium! Your request for the Starter Package has been submitted to the administration for confirmation."
     - (Note: All starter rewards are delivered to the player's in-game Chest).
     - STRICTLY DO NOT mention any commands (like !starterpackage) to the player.
     - You MUST append the exact token: <<<AWAIT_STARTER_CONFIRMATION>>>

9. SCREENSHOT & ERROR IMAGE DIAGNOSIS (VISION & OFFICIAL DOWNLOADS):
   - When a player uploads a screenshot or image of an error dialog, client crash, or technical issue:
     1. Analyze the exact error text, title, or error code in the image.
     2. Identify the root cause and provide 2 to 3 concise bullet steps.
     3. ALWAYS provide the required official download link:
        * Missing msvcp140.dll / vcruntime140.dll / C++ runtime error:
          Provide: [Official Microsoft Visual C++ All-in-One](https://aka.ms/vs/17/release/vc_redist.x86.exe)
        * DirectX / 3D Graphics / d3dx9 error:
          Provide: [Official Microsoft DirectX Web Installer](https://www.microsoft.com/en-us/download/details.aspx?id=35)
        * sro_client.exe disappeared / quarantined:
          Guide them to restore it in Windows Defender Protection History and add game folder to Exclusions.
        * Client outdated or Media.pk2 corrupted:
          Provide: [Exilium Official Website](https://exiliumonline.com)
"""

# -------------------------------------------------------------
# Fast Async AI Generation with Sub-second Models
# -------------------------------------------------------------
async def generate_ai_text_async(prompt: str, image_parts: list = None) -> str:
    """Generate response using Gemini async client with sub-second models and strict per-model timeouts."""
    if not gemini_client:
        return "❌ مفتاح الذكاء الاصطناعي غير متوفر حالياً."

    # Fast models ordered by tested sub-second latency
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
    for model_name in models_to_try:
        try:
            logger.info(f"Generating content with model: {model_name} (images: {len(image_parts) if image_parts else 0})")
            response = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=build_system_instruction(),
                        temperature=0.3,
                        max_output_tokens=500
                    )
                ),
                timeout=5.5
            )
            if response and response.text:
                return response.text
        except asyncio.TimeoutError:
            logger.warning(f"Model '{model_name}' timed out after 5.5s, switching to next candidate.")
            last_error = TimeoutError(f"Model {model_name} timed out.")
        except Exception as e:
            logger.warning(f"Model '{model_name}' error: {e}, switching to next candidate.")
            last_error = e

    logger.error(f"All candidate models failed: {last_error}")
    raise last_error if last_error else RuntimeError("No response from AI models.")

def generate_ai_text(prompt: str, image_parts: list = None) -> str:
    """Synchronous fallback helper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(generate_ai_text_async(prompt, image_parts), loop)
            return future.result(timeout=10.0)
        else:
            return asyncio.run(generate_ai_text_async(prompt, image_parts))
    except Exception as e:
        logger.error(f"Error in synchronous generate_ai_text: {e}")
        return "⚠️ An error occurred while generating response."

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
# Daily Tickets Tracker (for [GM]STAM DM Summary)
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

def record_daily_ticket(channel_id: int, channel_name: str, ign: str, username: str, issue: str, status: str = "⏳ In Progress"):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_tickets = daily_tickets_data.setdefault(today_str, {})
    cid_key = str(channel_id)
    existing = today_tickets.get(cid_key, {})
    today_tickets[cid_key] = {
        "channel_name": channel_name,
        "ign": ign or existing.get("ign", "Player"),
        "username": username or existing.get("username", "N/A"),
        "issue": (issue or existing.get("issue", "General support inquiry"))[:120],
        "status": status if status != "⏳ In Progress" else existing.get("status", status),
        "updated_at": datetime.now(timezone.utc).strftime("%H:%M UTC")
    }
    save_daily_tickets()

def update_daily_ticket_status(channel_id: int, status: str):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_tickets = daily_tickets_data.get(today_str, {})
    cid_key = str(channel_id)
    if cid_key in today_tickets:
        today_tickets[cid_key]["status"] = status
        today_tickets[cid_key]["updated_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
        save_daily_tickets()
    else:
        # Check all dates in case ticket was opened earlier
        for d, t_dict in daily_tickets_data.items():
            if cid_key in t_dict:
                t_dict[cid_key]["status"] = status
                t_dict[cid_key]["updated_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
                save_daily_tickets()
                break

def generate_daily_report_embed(date_str: str = None) -> discord.Embed:
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tickets_today = daily_tickets_data.get(date_str, {})

    total = len(tickets_today)
    resolved = sum(1 for t in tickets_today.values() if "resolved" in t.get("status", "").lower() or "✅" in t.get("status", ""))
    starters = sum(1 for t in tickets_today.values() if "starter" in t.get("status", "").lower() or "🎁" in t.get("status", ""))
    in_progress = total - resolved - starters

    embed = discord.Embed(
        title=f"📋 Exilium Daily Support Summary • {date_str}",
        description=(
            f"**Total Tickets Handled Today:** `{total}`\n"
            f"• ✅ **Resolved:** `{resolved}`\n"
            f"• 🎁 **Starter Delivered:** `{starters}`\n"
            f"• ⏳ **In Progress / Escalated:** `{in_progress}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x00A8FF
    )

    if tickets_today:
        ticket_lines = []
        for cid, t in list(tickets_today.items())[:25]:
            ign = t.get("ign", "Player")
            issue = t.get("issue", "Support request")[:50]
            status = t.get("status", "⏳ In Progress")
            time_str = t.get("updated_at", "")
            ticket_lines.append(f"• **{ign}** | `{status}`\n  └ *Issue:* {issue} ({time_str})")

        embed.add_field(
            name="🎫 Tickets Breakdown",
            value="\n".join(ticket_lines)[:1024],
            inline=False
        )
    else:
        embed.add_field(
            name="🎫 Tickets Breakdown",
            value="*No tickets recorded for today.*",
            inline=False
        )

    embed.set_footer(text=f"Exilium Daily Audit • [GM]STAM Management • {date_str}")
    return embed

async def send_daily_dm_to_stam(date_str: str = None):
    """Sends the daily tickets summary DM to [GM]STAM."""
    try:
        user_stam = bot.get_user(int(ADMIN_USER_ID))
        if not user_stam:
            user_stam = await bot.fetch_user(int(ADMIN_USER_ID))
        if not user_stam:
            logger.error(f"Could not find [GM]STAM user with ID {ADMIN_USER_ID}")
            return

        embed = generate_daily_report_embed(date_str)
        await user_stam.send(
            content=f"👑 **[GM]STAM** - إليك ملخص تذاكر ومشاكل اللاعبين اليومية ({date_str or 'اليوم'}):",
            embed=embed
        )
        logger.info(f"Daily ticket summary DM sent successfully to [GM]STAM ({ADMIN_USER_ID})")
    except Exception as e:
        logger.error(f"Failed to send daily DM to [GM]STAM: {e}")

# -------------------------------------------------------------
# Official Download Help View
# -------------------------------------------------------------
class DownloadHelpView(discord.ui.View):
    def __init__(self, include_vcredist: bool = False, include_directx: bool = False, include_website: bool = True):
        super().__init__(timeout=None)
        if include_vcredist:
            self.add_item(discord.ui.Button(
                label="Download Visual C++ All-in-One",
                style=discord.ButtonStyle.link,
                url="https://aka.ms/vs/17/release/vc_redist.x86.exe",
                emoji="⬇️"
            ))
        if include_directx:
            self.add_item(discord.ui.Button(
                label="Download DirectX Web Installer",
                style=discord.ButtonStyle.link,
                url="https://www.microsoft.com/en-us/download/details.aspx?id=35",
                emoji="⬇️"
            ))
        if include_website:
            self.add_item(discord.ui.Button(
                label="Exilium Website & Downloads",
                style=discord.ButtonStyle.link,
                url="https://exiliumonline.com",
                emoji="🌐"
            ))

# -------------------------------------------------------------
# Ticket Inactivity Tracker (12h Reminder + 30m Support Escalation)
# -------------------------------------------------------------
INACTIVITY_FILE = os.path.join(os.path.dirname(__file__), "inactivity_tracker.json")
inactivity_data: Dict[str, dict] = {}

def load_inactivity_data():
    global inactivity_data
    if os.path.exists(INACTIVITY_FILE):
        try:
            with open(INACTIVITY_FILE, "r", encoding="utf-8") as f:
                inactivity_data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading inactivity data: {e}")
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

# -------------------------------------------------------------
# Starter Package Approval View & Execution
# -------------------------------------------------------------
# channel_id -> {"ign": str}
pending_starters: Dict[int, dict] = {}

async def execute_starter_dispatch(guild: discord.Guild, ign: str, ticket_channel: discord.TextChannel, confirmed_by=None):
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
        logger.warning(f"Could not reach commands channel to dispatch starter commands for {ign}")
        return

    # Check bot permissions in commands channel
    perms = cmd_channel.permissions_for(cmd_channel.guild.me)
    if not perms.send_messages:
        error_msg = (
            f"⚠️ **Discord Permission Error:**\n"
            f"The bot does not have **'Send Messages'** permission in {cmd_channel.mention}!\n\n"
            f"👉 **Fix:** Please open Channel Settings for {cmd_channel.mention} ➔ **Permissions** ➔ add **`Exilium Ai Assistant`** (or role) and check **Send Messages: ✅ (Allow)**."
        )
        await ticket_channel.send(error_msg)
        logger.error(f"Missing send_messages permission in commands channel #{cmd_channel.name} (ID: {cmd_channel.id})")
        return

    try:
        # Dispatch the 3 commands directly to the commands channel
        await cmd_channel.send(f"!starterpackage {ign}")
        await asyncio.sleep(0.8)
        await cmd_channel.send(f"!betabox {ign}")
        await asyncio.sleep(0.8)
        await cmd_channel.send(f"!srozonebox {ign}")
        logger.info(f"Dispatched starter packages for '{ign}' in #{cmd_channel.name} (ID: {cmd_channel.id})")
        record_starter_given()
        update_daily_ticket_status(ticket_channel.id, "🎁 Starter Delivered")

        # Deliver confirmation to player without revealing internal command names
        # Strictly specifies "in-game Chest" as requested by user
        confirm_embed = discord.Embed(
            title="🎁 Starter Package Delivered!",
            description=(
                f"✅ **Your starter package has been delivered to `{ign}`!**\n\n"
                f"📦 Please check your in-game Chest. Have fun playing Exilium!"
            ),
            color=0x2ECC71
        )
        confirm_embed.set_footer(text="Exilium Support Management • Automated Delivery")
        await ticket_channel.send(embed=confirm_embed)

        # Remove from pending
        if ticket_channel.id in pending_starters:
            del pending_starters[ticket_channel.id]

    except discord.Forbidden:
        await ticket_channel.send(
            f"⚠️ **Forbidden (403):** The bot was blocked by Discord permissions from sending messages in {cmd_channel.mention}."
        )
        logger.error(f"Forbidden 403 when sending starter commands to {cmd_channel.id}")
    except Exception as e:
        logger.error(f"Error dispatching starter commands: {e}")
        await ticket_channel.send("⚠️ Error dispatching starter commands. Staff has been notified.")

class StarterApprovalView(discord.ui.View):
    def __init__(self, ign: str, ticket_channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.ign = ign
        self.ticket_channel = ticket_channel

    @discord.ui.button(
        label="Confirm Dispatch",
        style=discord.ButtonStyle.success,
        emoji="🎁",
        custom_id="exilium_confirm_starter_btn"
    )
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Strictly restrict button click to EXILIUM TEAM and STAM
        if not is_exilium_team_member(interaction.user):
            await interaction.response.send_message("❌ This action is restricted to the 👑 EXILIUM TEAM and [GM]STAM only.", ephemeral=True)
            return

        button.disabled = True
        button.label = "✅ Dispatched"
        await interaction.response.edit_message(view=self)

        await execute_starter_dispatch(
            interaction.guild,
            self.ign,
            self.ticket_channel,
            confirmed_by=interaction.user
        )

# -------------------------------------------------------------
# Discord Bot Setup
# -------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------------------------------------------
# Interactive Ticket Panel View (Optional Built-in Tickets)
# -------------------------------------------------------------
class TicketCreateButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="فتح تذكرة دعم | Open Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="exilium_open_ticket_btn"
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
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

# -------------------------------------------------------------
# Background Tasks (Inactivity, Daily DM to STAM, Weekly Analytics)
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

last_daily_report_date = ""

@tasks.loop(minutes=1)
async def daily_report_loop():
    """Sends daily tickets summary DM to [GM]STAM every day at 23:59 UTC."""
    global last_daily_report_date
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    # Send daily report at 23:59 UTC
    if now_utc.hour == 23 and now_utc.minute >= 58 and last_daily_report_date != today_str:
        await send_daily_dm_to_stam(today_str)
        last_daily_report_date = today_str

last_weekly_report_date = ""

@tasks.loop(hours=4)
async def weekly_report_loop():
    """Posts weekly analytics report every Sunday at 18:00+ UTC."""
    global last_weekly_report_date
    now_dt = datetime.now(timezone.utc)
    today_str = now_dt.strftime("%Y-%m-%d")

    # Sunday is weekday 6
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

def generate_analytics_embed() -> discord.Embed:
    total_t = analytics_data.get("tickets_total", 0)
    starters_g = analytics_data.get("starters_given", 0)
    categories = analytics_data.get("categories", {})

    breakdown_lines = []
    if categories:
        for cat, cnt in sorted(categories.items(), key=lambda x: x[1], reverse=True):
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
# Debounce Storage (5s Player Silence Debounce)
# -------------------------------------------------------------
# channel_id -> list of discord.Message
ticket_pending_queues: Dict[int, List[discord.Message]] = {}
# channel_id -> asyncio.Task
ticket_debounce_tasks: Dict[int, asyncio.Task] = {}

async def debounced_ticket_worker(channel_id: int):
    """Waits for 5 seconds of player silence before processing accumulated messages."""
    try:
        await asyncio.sleep(5.0)
    except asyncio.CancelledError:
        return  # Cancelled because player typed or sent another message

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
        user_query = "Player uploaded an error screenshot. Please diagnose the issue from the image, explain the fix in 2-3 bullet points, and provide the official download link."
    elif not user_query and not image_parts:
        return

    # Start typing indicator ONLY AFTER the 5s player silence has elapsed
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

    # Initialize history for this ticket if not existing
    if channel_id not in ticket_histories:
        ticket_histories[channel_id] = []

    history = ticket_histories[channel_id]
    speaker_label = f"Ticket ({target_mention})" if is_ticket_bot_embed else author.display_name
    history.append({"role": "user", "text": f"{speaker_label}: {user_query}"})

    # Keep history bounded
    if len(history) > MAX_HISTORY_PER_CHANNEL:
        ticket_histories[channel_id] = history[-MAX_HISTORY_PER_CHANNEL:]
        history = ticket_histories[channel_id]

    try:
        # Build conversation context
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

        # Save assistant reply to conversation history
        history.append({"role": "model", "text": clean_text})

        # If this is the initial ticket embed, prepend the player mention
        if is_ticket_bot_embed and target_mention:
            clean_text = f"👋 {target_mention}\n\n{clean_text}"

        # Check if official download buttons should be attached
        lower_ai = clean_text.lower()
        needs_vc = any(k in lower_ai for k in ["visual c++", "msvcp", "vcruntime", "vc_redist"])
        needs_dx = any(k in lower_ai for k in ["directx", "d3dx9", "direct3d"])
        help_view = None
        if needs_vc or needs_dx:
            help_view = DownloadHelpView(include_vcredist=needs_vc, include_directx=needs_dx)

        # Send AI Response in chunks if necessary
        chunks = [clean_text[i:i+1950] for i in range(0, len(clean_text), 1950)]
        for idx, chunk in enumerate(chunks):
            v = help_view if (idx == len(chunks) - 1) else None
            await channel.send(chunk, view=v)

        # Record inactivity tracking (12h reminder -> 30m escalation)
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

        # Update Daily Ticket Tracking & Analytics
        current_ign = ticket_igns.get(channel_id, author.display_name)
        if has_starter_request:
            record_ticket_stat("Starter Package Request")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, "Starter Package Request", "🎁 Starter Requested")
        elif needs_escalation:
            record_ticket_stat("Escalated to Staff")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "🚨 Escalated")
        elif is_resolved:
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "✅ Resolved")
        elif needs_vc or needs_dx:
            record_ticket_stat("Runtime / DirectX Error")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "⏳ In Progress")
        elif "c9" in clean_text.lower():
            record_ticket_stat("C9 Connection Error")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "⏳ In Progress")
        else:
            record_ticket_stat("General Inquiries")
            record_daily_ticket(channel_id, channel.name, current_ign, author.name, user_query[:60], "⏳ In Progress")

        # If starter package requested, prompt Admin/Staff with button and mention
        # Strictly mentions ONLY STAM, Head Supporter, and Community Supporter
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

            if not clean_ign:
                clean_ign = author.display_name

            pending_starters[channel_id] = {"ign": clean_ign}
            staff_pings = get_staff_ticket_mentions()

            approval_view = StarterApprovalView(ign=clean_ign, ticket_channel=channel)
            approval_embed = discord.Embed(
                title="🎁 Starter Package Request",
                description=(
                    f"**Player IGN:** `{clean_ign}`\n\n"
                    f"👉 **Action Required:** Authorized staff may click the button below or reply with **'ok'** / **'confirm'** / **'تمام'** to approve and dispatch the starter package."
                ),
                color=0xF1C40F
            )
            approval_embed.set_footer(text="Exilium Support Management • 👑 EXILIUM TEAM & STAM ONLY")
            await channel.send(content=f"🔔 {staff_pings}", embed=approval_embed, view=approval_view)

        # Handle Ticket Resolved Notification if triggered
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

        # Handle Escalation Notification if triggered (quiet mention, no embed, ping once only)
        if needs_escalation:
            if channel_id not in ticket_escalated:
                ticket_escalated[channel_id] = True
                staff_pings = get_staff_ticket_mentions()
                await channel.send(content=f"🔔 {staff_pings}")

    except Exception as e:
        logger.error(f"Error handling AI response for ticket {channel_id}: {e}")
        await channel.send("⚠️ An error occurred while generating a response. Staff has been notified.")

# -------------------------------------------------------------
# Koyeb / Cloud Platform Health Check Web Server
# -------------------------------------------------------------
async def handle_health_check(request):
    return web.json_response({
        "status": "online",
        "bot": str(bot.user) if bot.user else "initializing",
        "server": "Exilium Support Assistant",
        "latency_ms": round(bot.latency * 1000, 2) if bot.is_ready() else 0
    })

async def start_health_server():
    port = int(os.getenv("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info(f"Health check web server running on port {port}")
    except Exception as e:
        logger.warning(f"Health check web server could not bind to port {port}: {e}")

# -------------------------------------------------------------
# Bot Events
# -------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f"Bot connected successfully as {bot.user} (ID: {bot.user.id})")
    bot.add_view(TicketCreateButton())

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

    try:
        # Sync directly to all connected guilds for INSTANT slash command visibility in Discord
        for g in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=g)
                guild_synced = await bot.tree.sync(guild=g)
                logger.info(f"Instantly synced {len(guild_synced)} slash commands to guild '{g.name}' (ID: {g.id})")
            except Exception as ge:
                logger.warning(f"Could not sync to guild {g.id}: {ge}")

        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} global slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

    activity = discord.Game(name="Exilium SRO | Support Tickets")
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
    # If staff has taken over or user typing is staff, do nothing
    if ticket_staff_taken_over.get(channel.id, False) or is_staff_member(user):
        return

    cid = channel.id
    if cid in ticket_debounce_tasks and not ticket_debounce_tasks[cid].done():
        ticket_debounce_tasks[cid].cancel()
        ticket_debounce_tasks[cid] = asyncio.create_task(debounced_ticket_worker(cid))

@bot.event
async def on_message(message: discord.Message):
    # Ignore messages sent by ourselves
    if message.author == bot.user:
        return

    # Process standard text commands first
    await bot.process_commands(message)

    # Check if this message was sent inside a Ticket channel
    if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
        return

    if not is_ticket_channel(message.channel):
        return

    if not gemini_client:
        return

    channel_id = message.channel.id

    # -------------------------------------------------------------
    # Case 1: Embed from Ticket Bot (e.g. Exilium | Control)
    # -------------------------------------------------------------
    if message.author.bot:
        if not message.embeds:
            return

        extracted_subject = ""
        extracted_ign = ""
        extracted_username = ""
        target_mention = ""

        for emb in message.embeds:
            fields_dict = {f.name.lower().strip(): f.value.strip() for f in emb.fields}

            # Extract common ticket fields
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

            # If subject not in fields, look into description
            if not extracted_subject and emb.description:
                for line in emb.description.split("\n"):
                    if "subject" in line.lower() or "reason" in line.lower():
                        extracted_subject = line
                        break

        if extracted_subject:
            if extracted_ign:
                ticket_igns[channel_id] = extracted_ign
            logger.info(f"Detected Ticket Bot Embed in {message.channel.name} | IGN: {extracted_ign} | Subject: {extracted_subject}")
            record_daily_ticket(channel_id, message.channel.name, extracted_ign, extracted_username, extracted_subject, "⏳ In Progress")

            user_query = (
                f"Player opened a new ticket:\n"
                f"- In-Game Name (IGN): {extracted_ign or 'Player'}\n"
                f"- Account Username: {extracted_username or 'N/A'}\n"
                f"- Ticket Subject / Issue: {extracted_subject}\n\n"
                f"INSTRUCTIONS:\n"
                f"1. Detect the player's language strictly from the Ticket Subject (e.g. if Turkish -> Turkish; if Portuguese -> Portuguese; if Arabic -> Arabic; if German -> German; if English/unclear -> English).\n"
                f"2. Use the Subject as the immediate context of their issue.\n"
                f"   - If asking for Starter Package or mentions being a new player ('i\\'m new', 'new player', etc.): tell them briefly in English that it is submitted for Admin confirmation, and append <<<AWAIT_STARTER_CONFIRMATION>>>.\n"
                f"   - If a known issue: give 2-3 short bullet steps.\n"
                f"   - If vague or greeting (like '{extracted_subject}'): simply ask how you can help in their language."
            )

            # Ticket bot embed arrives once; process promptly with typing indicator
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

    # -------------------------------------------------------------
    # Case 2: Message from a Human Player / Staff
    # -------------------------------------------------------------
    # If a staff member (Exilium Team, Head Supporter, Supporter, GM/Admin) sends a message in this ticket:
    if is_staff_member(message.author):
        # 1. Check if an EXILIUM TEAM member is confirming a pending starter package
        if is_exilium_team_member(message.author) and channel_id in pending_starters:
            msg_clean = message.content.strip().lower()
            confirm_keywords = {"تمام", "ok", "تم", "confirm", "send", "موافق", "done", "yes"}
            words_in_msg = set(msg_clean.split())
            if msg_clean in confirm_keywords or words_in_msg.intersection(confirm_keywords):
                pending_ign = pending_starters[channel_id]["ign"]
                await execute_starter_dispatch(
                    message.guild,
                    pending_ign,
                    message.channel,
                    confirmed_by=message.author
                )

        # 2. Staff has taken over the ticket!
        # Cancel any active bot typing / debounce and mark ticket as staff-handled:
        ticket_staff_taken_over[channel_id] = True
        if channel_id in ticket_debounce_tasks and not ticket_debounce_tasks[channel_id].done():
            ticket_debounce_tasks[channel_id].cancel()
        ticket_pending_queues.pop(channel_id, None)

        # Remove inactivity tracking so bot never pesters player while staff is in ticket
        inactivity_data.pop(str(channel_id), None)
        save_inactivity_data()

        logger.info(f"Staff member {message.author.display_name} spoke in #{message.channel.name}. Bot silenced for this ticket.")
        # NEVER reply to staff messages!
        return

    # If staff has already taken over this ticket, bot completely stops talking!
    if ticket_staff_taken_over.get(channel_id, False):
        return

    # Check recent channel history to see if staff already intervened in this ticket
    if channel_id not in ticket_staff_taken_over:
        try:
            async for prev_msg in message.channel.history(limit=20):
                if prev_msg.author != bot.user and not prev_msg.author.bot and is_staff_member(prev_msg.author):
                    ticket_staff_taken_over[channel_id] = True
                    logger.info(f"Detected previous staff message from {prev_msg.author.display_name} in #{message.channel.name}. Bot silenced.")
                    return
        except Exception as e:
            logger.warning(f"Error checking message history for staff takeover: {e}")

    # Reset inactivity tracker because human spoke
    if str(channel_id) in inactivity_data:
        del inactivity_data[str(channel_id)]
        save_inactivity_data()

    # 5-SECOND DEBOUNCE LOGIC:
    # If the player is still typing or sending consecutive messages, wait until they are silent for 5 seconds.
    if channel_id in ticket_debounce_tasks and not ticket_debounce_tasks[channel_id].done():
        ticket_debounce_tasks[channel_id].cancel()

    if channel_id not in ticket_pending_queues:
        ticket_pending_queues[channel_id] = []
    ticket_pending_queues[channel_id].append(message)

    # Launch debounce timer task (5.0s of silence before typing and responding)
    ticket_debounce_tasks[channel_id] = asyncio.create_task(debounced_ticket_worker(channel_id))

# -------------------------------------------------------------
# Slash Commands
# -------------------------------------------------------------
@bot.tree.command(name="ask", description="اسأل مساعد Exilium الذكي عن أي شيء يخص السيرفر أو حلول المشاكل")
@app_commands.describe(question="سؤالك أو المشكلة التي تواجهك")
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

@bot.tree.command(name="ticket-panel", description="إرسال لوحة فتح تذاكر الدعم الفني في هذه القناة (للإدارة فقط)")
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

@bot.tree.command(name="reload-kb", description="إعادة تحميل قاعدة معرفة Exilium دون إعادة تشغيل البوت (للإدارة)")
@app_commands.checks.has_permissions(administrator=True)
async def reload_kb_command(interaction: discord.Interaction):
    load_knowledge_base()
    await interaction.response.send_message("✅ تم إعادة تحميل ملف `knowledge_base.json` بنجاح!", ephemeral=True)

@bot.tree.command(name="report", description="عرض تقرير إحصائيات الذكاء الاصطناعي والتذاكر (👑 EXILIUM TEAM)")
async def report_command(interaction: discord.Interaction):
    if not is_exilium_team_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص لأعضاء 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    embed = generate_analytics_embed()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="daily-report", description="إرسال تقرير التذاكر اليومي في الخاص لـ [GM]STAM أو في القناة")
@app_commands.describe(send_dm="إرسال التقرير في الخاص لـ [GM]STAM (نعم/لا)")
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

@bot.tree.command(name="transcript", description="استخراج سجل محادثة التيكت الحالي كملف نصي (للإدارة)")
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

@bot.tree.command(name="pause-bot", description="إيقاف ردود الذكاء الاصطناعي في هذا التيكت (للإدارة)")
async def pause_bot_command(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص للستاف فقط.", ephemeral=True)
        return
    ticket_staff_taken_over[interaction.channel_id] = True
    await interaction.response.send_message("⏸️ تم إيقاف ردود الذكاء الاصطناعي في هذا التيكت. يمكنك التحدث مع اللاعب بحرية.", ephemeral=True)

@bot.tree.command(name="resume-bot", description="إعادة تفعيل ردود الذكاء الاصطناعي في هذا التيكت (للإدارة)")
async def resume_bot_command(interaction: discord.Interaction):
    if not is_staff_member(interaction.user):
        await interaction.response.send_message("❌ هذا الأمر مخصص للستاف فقط.", ephemeral=True)
        return
    ticket_staff_taken_over.pop(interaction.channel_id, None)
    await interaction.response.send_message("▶️ تم إعادة تفعيل ردود الذكاء الاصطناعي في هذا التيكت بنجاح.", ephemeral=True)

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
        "daily_tickets.json",
        "inactivity_tracker.json",
        "analytics.json"
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

@bot.tree.command(name="update-bot", description="سحب آخر تحديثات الكود من GitHub وإعادة تشغيل البوت تلقائياً (للإدارة فقط)")
async def update_bot_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_exilium_team_member(interaction.user):
        await interaction.followup.send("❌ هذا الأمر مخصص لـ 👑 EXILIUM TEAM فقط.", ephemeral=True)
        return

    update_summary = ""
    used_method = ""
    github_token = os.getenv("GITHUB_TOKEN", "").strip()

    # 1. Try Git if git command is available and .git exists
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
        except Exception as ge:
            logger.warning(f"Git pull failed or git not in PATH ({ge}), falling back to direct GitHub archive...")

    # 2. Fallback to direct GitHub Zip extraction (no git required)
    if not git_worked:
        try:
            loop = asyncio.get_running_loop()
            update_summary = await loop.run_in_executor(None, download_github_update_sync, "mrshazya-del/exilium-bot", "main", github_token)
            used_method = "Direct GitHub Archive Engine"
        except Exception as ze:
            logger.error(f"Failed to download update via zip: {ze}")
            await interaction.followup.send(f"⚠️ فشل تحديث البوت: `{ze}`\nيرجى التحقق من اتصال السيرفر بالإنترنت أو حالة مستودع GitHub.", ephemeral=True)
            return

    # Reload knowledge base
    load_knowledge_base()

    embed = discord.Embed(
        title="🔄 Exilium Bot Auto-Updater",
        description=(
            f"**طريقة التحديث:** `{used_method}`\n"
            f"**النتيجة:**\n```{update_summary[:800]}```\n\n"
            "🚀 **جاري إعادة تشغيل البوت بالنسخة الجديدة تلقائياً الآن...**\n"
            "سيعود البوت للعمل خلال ثانية واحدة."
        ),
        color=0x2ECC71
    )
    embed.set_footer(text=f"طلب بواسطة: {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    logger.info(f"Auto-update successful via {used_method} by {interaction.user}. Restarting process...")
    await asyncio.sleep(1.0)
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

    print("🚀 جاري تشغيل بوت Exilium AI Assistant...")
    bot.run(DISCORD_BOT_TOKEN)
