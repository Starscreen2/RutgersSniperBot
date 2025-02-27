import discord
import asyncio
import requests
import sqlite3
import os
import time
import psutil
from discord import app_commands
from discord.ext import commands
from typing import Optional

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "snipes.db")
RUTGERS_API_URL = "https://sis.rutgers.edu/soc/api/courses.json?year=2025&term=7&campus=NB"
SCAN_INTERVAL = 1 #checks every second

COURSE_CACHE = {"timestamp": 0, "data": None}
CACHE_DURATION = 60  

ADMIN_ID = "admin_id"

GLOBAL_SNIPING_ENABLED = False
ADMIN_GLOBAL_LAST_OPEN_STATUS = {}

ADMIN_SCAN_NOTIFY = False
ADMIN_SCAN_LAST_NOTIFIED = 0
ADMIN_SCAN_NOTIFY_COOLDOWN = 60 


allocated_memory = []

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

#########################################
# Database Initialization and Helpers   #
#########################################

async def initialize_storage():
    os.makedirs("data", exist_ok=True)
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS snipes (
                discord_id TEXT,
                index_number TEXT,
                notifications_sent INTEGER DEFAULT 0,
                UNIQUE(discord_id, index_number)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_configs (
                discord_id TEXT PRIMARY KEY,
                max_snipes INTEGER DEFAULT 10,
                banned INTEGER DEFAULT 0,
                is_mod INTEGER DEFAULT 0,
                notif_limit INTEGER DEFAULT 5,
                tts_enabled INTEGER DEFAULT 0
            )
        """)
        c.execute("PRAGMA table_info(user_configs)")
        columns = [row[1] for row in c.fetchall()]
        if "notif_limit" not in columns:
            c.execute("ALTER TABLE user_configs ADD COLUMN notif_limit INTEGER DEFAULT 5")
        if "tts_enabled" not in columns:
            c.execute("ALTER TABLE user_configs ADD COLUMN tts_enabled INTEGER DEFAULT 0")
        conn.commit()

def get_user_config(discord_id):
    """Retrieve or create a default config for the given user."""
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT max_snipes, banned, is_mod, notif_limit, tts_enabled FROM user_configs WHERE discord_id = ?", (discord_id,))
        row = c.fetchone()
        if row is None:
            c.execute("INSERT INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, 5, 0)", (discord_id,))
            conn.commit()
            return (10, 0, 0, 5, 0)
        return row

#########################################
# Course Cache and Utility Functions    #
#########################################

def get_cached_courses():
    """Retrieve courses using a cache to lower network overhead."""
    current_time = time.time()
    if (COURSE_CACHE["data"] is None or 
        (current_time - COURSE_CACHE["timestamp"]) > CACHE_DURATION):
        try:
            response = requests.get(RUTGERS_API_URL)
            if response.status_code == 200:
                COURSE_CACHE["data"] = response.json()
                COURSE_CACHE["timestamp"] = current_time
            else:
                print("❌ API returned non-200 status:", response.status_code)
                return []
        except Exception as e:
            print("🔥 API request failed:", e)
            return []
    return COURSE_CACHE["data"]

async def fetch_courses():
    return get_cached_courses()

def get_course_name(index_number):
    courses = get_cached_courses()
    for course in courses:
        course_title = course.get("title", "Unknown Course")
        subject = course.get("subject", "Unknown Subject")
        course_number = course.get("courseNumber", "XXX")
        for section in course.get("sections", []):
            if str(section.get("index")) == str(index_number):
                return f"{subject} {course_number} - {course_title}"
    return f"Unknown Course ({index_number})"

#########################################
# Snipe Management Functions            #
#########################################

async def add_snipe(discord_id, index_number):
    max_snipes, banned, is_mod, notif_limit, tts_enabled = get_user_config(discord_id)
    if int(banned) == 1:
        return "banned"
    if discord_id != ADMIN_ID and int(is_mod) != 1:
        with sqlite3.connect(SQL_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM snipes WHERE discord_id = ?", (discord_id,))
            count = c.fetchone()[0]
            if count >= max_snipes:
                return False  # Limit reached
    try:
        with sqlite3.connect(SQL_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO snipes (discord_id, index_number, notifications_sent)
                VALUES (?, ?, 0)
            """, (discord_id, index_number))
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return "duplicate"  # Already exists

async def notify_users(index_number):
    print(f"🔍 Notifying users for course {index_number}...")
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT discord_id, notifications_sent
            FROM snipes
            WHERE index_number = ?
        """, (index_number,))
        users = c.fetchall()

        for user_id, sent_count in users:
            _, _, _, notif_limit, tts_enabled = get_user_config(user_id)
            if sent_count < notif_limit:
                try:
                    user = await bot.fetch_user(int(user_id))
                    if user:
                        course_name = get_course_name(index_number)
                        await user.send(
                            f"🔔 {user.mention}, the course **{course_name}** (index {index_number}) is now OPEN! (Notification {sent_count + 1}/{notif_limit})",
                            tts=(tts_enabled == 1)
                        )
                        print(f"✅ Sent DM to user {user_id} (#{sent_count+1})")
                except discord.HTTPException as e:
                    print(f"❌ Failed to send message to {user_id}: {e}")

                c.execute("""
                    UPDATE snipes
                    SET notifications_sent = notifications_sent + 1
                    WHERE discord_id = ? AND index_number = ?
                """, (user_id, index_number))

                if sent_count + 1 >= notif_limit:
                    c.execute("""
                        DELETE FROM snipes
                        WHERE discord_id = ? AND index_number = ?
                    """, (user_id, index_number))
                    print(f"🗑 Deleted snipe for {user_id} - {index_number} after {notif_limit} notifications.")

        conn.commit()

async def check_courses():
    global ADMIN_SCAN_LAST_NOTIFIED
    while True:
        try:
            print("🔄 Checking courses...")
            with sqlite3.connect(SQL_FILE) as conn:
                c = conn.cursor()
                c.execute("SELECT DISTINCT index_number FROM snipes")
                tracked_courses = {row[0] for row in c.fetchall()}

            courses = await fetch_courses()
            open_sections = 0
            for course in courses:
                for section in course.get("sections", []):
                    index_number = section.get("index")
                    status = str(section.get("openStatus")).strip().upper()
                    if status == "TRUE":
                        open_sections += 1

                    print(f"🔎 Course {index_number}: {status}")

                    if str(index_number) in tracked_courses and status == "TRUE":
                        print(f"✅ Course {index_number} is OPEN! Notifying users...")
                        await notify_users(index_number)

                    if GLOBAL_SNIPING_ENABLED:
                        course_key = str(index_number)
                        current_open = (status == "TRUE")
                        prev_open = ADMIN_GLOBAL_LAST_OPEN_STATUS.get(course_key, None)
                        if prev_open is None:
                            ADMIN_GLOBAL_LAST_OPEN_STATUS[course_key] = current_open
                        elif prev_open != current_open:
                            try:
                                admin_user = await bot.fetch_user(int(ADMIN_ID))
                                course_name = get_course_name(index_number)
                                state = "opened" if current_open else "closed"
                                await admin_user.send(
                                    f"🌐 Global Snipe Alert: **{course_name}** (Index: {index_number}) just {state}!"
                                )
                                print(f"✅ Notified admin about course {index_number} state change to {state}.")
                            except Exception as e:
                                print(f"❌ Failed to notify admin for course {index_number}: {e}")
                            ADMIN_GLOBAL_LAST_OPEN_STATUS[course_key] = current_open
            if ADMIN_SCAN_NOTIFY:
                now = time.time()
                if now - ADMIN_SCAN_LAST_NOTIFIED >= ADMIN_SCAN_NOTIFY_COOLDOWN:
                    try:
                        admin_user = await bot.fetch_user(int(ADMIN_ID))
                        await admin_user.send(
                            f"🔄 API Scan completed at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}. "
                            f"Open sections: {open_sections}."
                        )
                    except Exception as e:
                        print(f"❌ Failed to send scan notification to admin: {e}")
                    ADMIN_SCAN_LAST_NOTIFIED = now

            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"🔥 check_courses() crashed: {e}")

#########################################
# Helper: Generate Admin Status Message #
#########################################

async def get_admin_status_message():
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM snipes")
        active_snipes_count = c.fetchone()[0]

    last_scan_time = COURSE_CACHE["timestamp"]
    last_scan_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_scan_time)) if last_scan_time else "Never"

    open_sections = 0
    courses = get_cached_courses()
    for course in courses:
        for section in course.get("sections", []):
            if str(section.get("openStatus")).strip().upper() == "TRUE":
                open_sections += 1

    process = psutil.Process(os.getpid())
    mem_usage_mb = process.memory_info().rss / (1024 * 1024)
    artificial_mem_mb = len(allocated_memory)

    status_message = (
        f"**Bot Status:**\n"
        f"- Global Sniping Enabled: {GLOBAL_SNIPING_ENABLED}\n"
        f"- Scan Notifications Enabled: {ADMIN_SCAN_NOTIFY}\n"
        f"- Last API Scan: {last_scan_str}\n"
        f"- Open Sections (per API): {open_sections}\n"
        f"- Active User Snipes: {active_snipes_count}\n"
        f"- RAM Usage: {mem_usage_mb:.2f} MB\n"
        f"- Artificial Memory Allocated: {artificial_mem_mb} MB"
    )
    return status_message

#########################################
# Helper: Fetch a user from an identifier
#########################################

async def fetch_user_by_identifier(user_identifier: str) -> Optional[discord.User]:
    try:
        return await bot.fetch_user(int(user_identifier))
    except ValueError:
        for guild in bot.guilds:
            member = guild.get_member_named(user_identifier)
            if member:
                return member
    return None

#########################################
# Slash Commands (User)                 #
#########################################

@bot.tree.command(name="snipe", description="Add a course to your snipes.")
async def snipe(interaction: discord.Interaction, index_number: str):
    result = await add_snipe(str(interaction.user.id), index_number)
    course_name = get_course_name(index_number)
    if result is True:
        await interaction.response.send_message(f"✅ {interaction.user.mention}, you'll be notified when **{course_name}** (index {index_number}) opens!")
    elif result == "duplicate":
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, you're already sniping **{course_name}** (index {index_number})!")
    elif result == "banned":
        await interaction.response.send_message("❌ You are banned from using the bot.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ You have reached your snipe limit.", ephemeral=True)
snipe.dm_permission = True

@bot.tree.command(name="my_snipes", description="List your active snipes with course names.")
async def my_snipes(interaction: discord.Interaction):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT index_number FROM snipes WHERE discord_id = ?", (str(interaction.user.id),))
        snipes = [row[0] for row in c.fetchall()]
    if snipes:
        snipes_list = [f"({get_course_name(index_num)})" for index_num in snipes]
        snipes_str = ", \n".join(snipes_list)
        await interaction.response.send_message(f"📋 {interaction.user.mention}\nYour active snipes:\n{snipes_str}")
    else:
        await interaction.response.send_message(f"ℹ️ {interaction.user.mention}, you have no active snipes.")
my_snipes.dm_permission = True

@bot.tree.command(name="clear_snipes", description="Remove all your snipes.")
async def clear_snipes(interaction: discord.Interaction):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM snipes WHERE discord_id = ?", (str(interaction.user.id),))
        conn.commit()
    await interaction.response.send_message(f"🗑 {interaction.user.mention}, all your snipes have been removed!")
clear_snipes.dm_permission = True

@bot.tree.command(name="remove_snipe", description="Remove a specific snipe.")
async def remove_snipe(interaction: discord.Interaction, index_number: str):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM snipes WHERE discord_id = ? AND index_number = ?", (str(interaction.user.id), index_number))
        conn.commit()
    await interaction.response.send_message(f"✅ {interaction.user.mention}, removed snipe for index **{index_number}**!")
remove_snipe.dm_permission = True

@bot.tree.command(name="set_notif_limit", description="Set the number of notifications you'll receive per course (default is 5).")
async def set_notif_limit(interaction: discord.Interaction, limit: int):
    if limit < 1 or limit > 20:
        await interaction.response.send_message("❌ Please choose a notification limit between 1 and 20.", ephemeral=True)
        return
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, ?, 0)", (str(interaction.user.id), limit))
        c.execute("UPDATE user_configs SET notif_limit = ? WHERE discord_id = ?", (limit, str(interaction.user.id)))
        conn.commit()
    await interaction.response.send_message(f"✅ {interaction.user.mention}, your notification limit has been set to {limit} per course.", ephemeral=True)
set_notif_limit.dm_permission = True

@bot.tree.command(name="set_tts", description="Toggle TTS for open section notifications (default off).")
async def set_tts(interaction: discord.Interaction, enable: bool):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, 5, ?)", (str(interaction.user.id), int(enable)))
        c.execute("UPDATE user_configs SET tts_enabled = ? WHERE discord_id = ?", (int(enable), str(interaction.user.id)))
        conn.commit()
    await interaction.response.send_message(f"✅ {interaction.user.mention}, TTS for open section notifications has been {'enabled' if enable else 'disabled'}.", ephemeral=True)
set_tts.dm_permission = True

@bot.tree.command(name="commands", description="List available commands.")
async def commands_help(interaction: discord.Interaction):
    help_message = """📖 **Sniper Bot Commands:**
```
/snipe <index_number>        → Add a course to your snipes.
/my_snipes                   → List your active snipes with course names.
/remove_snipe <index_number> → Remove a specific snipe.
/clear_snipes                → Remove all your snipes.
/set_notif_limit <limit>     → Set the number of notifications you'll receive per course.
/set_tts <enable>            → Toggle TTS for open section notifications (true/false).
```
Default notifications per course: 5. 🚀
    """
    await interaction.response.send_message(help_message)
commands_help.dm_permission = True

#########################################
# Admin Check for Commands              #
#########################################

async def admin_check(interaction: discord.Interaction) -> bool:
    if str(interaction.user.id) == ADMIN_ID:
        return True
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT is_mod FROM user_configs WHERE discord_id = ?", (str(interaction.user.id),))
        row = c.fetchone()
        if row and row[0] == 1:
            return True
    raise app_commands.CheckFailure("You don't have permission to use this command.")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        except Exception:
            pass
    else:
        try:
            await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)
        except Exception:
            pass

#########################################
# Slash Commands (Admin)                #
#########################################

@bot.tree.command(name="admin_list_snipes", description="List all active snipes in a .txt file.")
@app_commands.check(admin_check)
async def admin_list_snipes(interaction: discord.Interaction):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id, index_number, notifications_sent FROM snipes")
        rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No active snipes.", ephemeral=True)
        return
    lines = []
    for discord_id, index_number, notifications_sent in rows:
        try:
            user = await bot.fetch_user(int(discord_id))
            username = f"{user.name}#{user.discriminator}"
        except Exception:
            username = discord_id
        course_name = get_course_name(index_number)
        lines.append(f"User: {username} (ID: {discord_id}) | Course: {course_name} (Index: {index_number}) | Notifications Sent: {notifications_sent}")
    file_path = "admin_snipes.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    await interaction.response.send_message(file=discord.File(file_path), ephemeral=True)
    os.remove(file_path)
admin_list_snipes.dm_permission = True

@bot.tree.command(name="admin_edit_limit", description="Edit a user's snipes limit.")
@app_commands.check(admin_check)
async def admin_edit_limit(interaction: discord.Interaction, member: discord.User, limit: int, message: Optional[str] = None):
    if str(member.id) == ADMIN_ID:
        await interaction.response.send_message("❌ Cannot change limit for the admin.", ephemeral=True)
        return
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT is_mod FROM user_configs WHERE discord_id = ?", (str(member.id),))
        row = c.fetchone()
        if row and row[0] == 1:
            await interaction.response.send_message("❌ Cannot change limit for a mod; they have unlimited snipes.", ephemeral=True)
            return
        c.execute("INSERT OR IGNORE INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, 5, 0)", (str(member.id),))
        c.execute("UPDATE user_configs SET max_snipes = ? WHERE discord_id = ?", (limit, str(member.id)))
        conn.commit()
    await interaction.response.send_message(f"✅ Set snipes limit for {member.mention} to {limit}.", ephemeral=True)
    if message:
        try:
            await member.send(f"Admin Message: {message}")
        except Exception:
            await interaction.followup.send(f"⚠️ Couldn't send DM to {member.mention}.", ephemeral=True)
admin_edit_limit.dm_permission = True

@bot.tree.command(name="admin_ban", description="Ban a user from using the bot using their ID or username.")
@app_commands.check(admin_check)
async def admin_ban(interaction: discord.Interaction, user_identifier: str, message: Optional[str] = None):
    user = await fetch_user_by_identifier(user_identifier)
    if user is None:
        await interaction.response.send_message("❌ Could not find a user with that identifier.", ephemeral=True)
        return
    if str(user.id) == ADMIN_ID:
        await interaction.response.send_message("❌ Cannot ban the admin.", ephemeral=True)
        return
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, 5, 0)", (str(user.id),))
        c.execute("UPDATE user_configs SET banned = 1 WHERE discord_id = ?", (str(user.id),))
        conn.commit()
    await interaction.response.send_message(f"✅ Banned {user.mention}.", ephemeral=True)
    if message:
        try:
            await user.send(f"You have been banned from using the bot. Reason: {message}")
        except Exception:
            await interaction.followup.send(f"⚠️ Couldn't send DM to {user.mention}.", ephemeral=True)
admin_ban.dm_permission = True

@bot.tree.command(name="admin_unban", description="Unban a user from using the bot using their ID or username.")
@app_commands.check(admin_check)
async def admin_unban(interaction: discord.Interaction, user_identifier: str):
    user = await fetch_user_by_identifier(user_identifier)
    if user is None:
        await interaction.response.send_message("❌ Could not find a user with that identifier.", ephemeral=True)
        return
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, 5, 0)", (str(user.id),))
        c.execute("UPDATE user_configs SET banned = 0 WHERE discord_id = ?", (str(user.id),))
        conn.commit()
    await interaction.response.send_message(f"✅ Unbanned {user.mention}.", ephemeral=True)
admin_unban.dm_permission = True

@bot.tree.command(name="admin_set_mod", description="Give a user mod privileges.")
@app_commands.check(admin_check)
async def admin_set_mod(interaction: discord.Interaction, member: discord.User):
    if str(member.id) == ADMIN_ID:
        await interaction.response.send_message("❌ Admin is already mod by default.", ephemeral=True)
        return
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_configs (discord_id, max_snipes, banned, is_mod, notif_limit, tts_enabled) VALUES (?, 10, 0, 0, 5, 0)", (str(member.id),))
        c.execute("UPDATE user_configs SET is_mod = 1 WHERE discord_id = ?", (str(member.id),))
        conn.commit()
    await interaction.response.send_message(f"✅ Set {member.mention} as a mod.", ephemeral=True)
admin_set_mod.dm_permission = True

@bot.tree.command(name="admin_remove_mod", description="Remove mod privileges from a user.")
@app_commands.check(admin_check)
async def admin_remove_mod(interaction: discord.Interaction, member: discord.User):
    if str(member.id) == ADMIN_ID:
        await interaction.response.send_message("❌ Cannot remove admin.", ephemeral=True)
        return
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("UPDATE user_configs SET is_mod = 0 WHERE discord_id = ?", (str(member.id),))
        conn.commit()
    await interaction.response.send_message(f"✅ Removed mod privileges from {member.mention}.", ephemeral=True)
admin_remove_mod.dm_permission = True

@bot.tree.command(name="admin_list_mods", description="List all mods with their usernames.")
@app_commands.check(admin_check)
async def admin_list_mods(interaction: discord.Interaction):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id FROM user_configs WHERE is_mod = 1")
        rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No mods found.", ephemeral=True)
        return
    lines = []
    for (discord_id,) in rows:
        try:
            user = await bot.fetch_user(int(discord_id))
            username = f"{user.name}#{user.discriminator}"
        except Exception:
            username = discord_id
        lines.append(f"{username} (ID: {discord_id})")
    await interaction.response.send_message("**Mods:**\n" + "\n".join(lines), ephemeral=True)
admin_list_mods.dm_permission = True

@bot.tree.command(
    name="admin_status", 
    description="Show bot status information (admin only)."
)
@app_commands.check(admin_check)
async def admin_status(interaction: discord.Interaction):
    status_message = await get_admin_status_message()
    await interaction.response.send_message(status_message, ephemeral=True)
admin_status.default_member_permissions = discord.Permissions(administrator=True)
admin_status.dm_permission = True

@bot.tree.command(name="admin_toggle_scan_notify", description=f"Toggle API scan notifications for admin (with a {ADMIN_SCAN_NOTIFY_COOLDOWN}-second cooldown).")
@app_commands.check(admin_check)
async def admin_toggle_scan_notify(interaction: discord.Interaction, enable: bool):
    global ADMIN_SCAN_NOTIFY
    ADMIN_SCAN_NOTIFY = enable
    await interaction.response.send_message(f"Scan notifications have been {'enabled' if enable else 'disabled'}.", ephemeral=True)
admin_toggle_scan_notify.dm_permission = True

@bot.tree.command(name="admin_global_snipe", description="Toggle global sniping mode for the admin.")
@app_commands.check(admin_check)
async def admin_global_snipe(interaction: discord.Interaction, enable: bool):
    global GLOBAL_SNIPING_ENABLED, ADMIN_GLOBAL_LAST_OPEN_STATUS
    GLOBAL_SNIPING_ENABLED = enable
    if enable:
        courses = get_cached_courses()
        for course in courses:
            for section in course.get("sections", []):
                course_key = str(section.get("index"))
                ADMIN_GLOBAL_LAST_OPEN_STATUS[course_key] = (str(section.get("openStatus")).strip().upper() == "TRUE")
    else:
        ADMIN_GLOBAL_LAST_OPEN_STATUS = {}
    await interaction.response.send_message(f"Global sniping mode has been {'enabled' if enable else 'disabled'}.", ephemeral=True)
admin_global_snipe.dm_permission = True

@bot.tree.command(name="admin_show_banned", description="Display a list of all banned users.")
@app_commands.check(admin_check)
async def admin_show_banned(interaction: discord.Interaction):
    with sqlite3.connect(SQL_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT discord_id FROM user_configs WHERE banned = 1")
        rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("No banned users.", ephemeral=True)
        return
    banned_users = []
    for (discord_id,) in rows:
        try:
            user = await bot.fetch_user(int(discord_id))
            banned_users.append(f"{user.name}#{user.discriminator} (ID: {discord_id})")
        except Exception:
            banned_users.append(f"Unknown User (ID: {discord_id})")
    message = "**Banned Users:**\n" + "\n".join(banned_users)
    await interaction.response.send_message(message, ephemeral=True)
admin_show_banned.dm_permission = True

@bot.tree.command(name="admin_set_ram", description="Artificially allocate memory (in MB) for testing.")
@app_commands.check(admin_check)
async def admin_set_ram(interaction: discord.Interaction, megabytes: int):
    global allocated_memory
    allocated_memory.clear()
    try:
        block = "X" * (1024 * 1024)  # ~1 MB block
        for _ in range(megabytes):
            allocated_memory.append(block)
        await interaction.response.send_message(f"✅ Allocated approximately {megabytes} MB of artificial memory.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error allocating memory: {e}", ephemeral=True)
admin_set_ram.dm_permission = True

@bot.tree.command(name="admin_unset_ram", description="Free the artificially allocated memory.")
@app_commands.check(admin_check)
async def admin_unset_ram(interaction: discord.Interaction):
    global allocated_memory
    allocated_memory.clear()
    await interaction.response.send_message("✅ Artificial memory has been freed.", ephemeral=True)
admin_unset_ram.dm_permission = True

@bot.tree.command(name="admin_help", description="Display all admin commands.")
@app_commands.check(admin_check)
async def admin_help(interaction: discord.Interaction):
    help_message = """
**Admin Commands:**
`/admin_list_snipes` - List all active snipes in a .txt file.
`/admin_edit_limit <user> <limit> [message]` - Edit a user's snipes limit.
`/admin_ban <user_identifier> [message]` - Ban a user from using the bot (by ID or username).
`/admin_unban <user_identifier>` - Unban a user (by ID or username).
`/admin_set_mod <user>` - Give a user mod privileges.
`/admin_remove_mod <user>` - Remove mod privileges from a user.
`/admin_list_mods` - List all mods.
`/admin_show_banned` - List all banned users.
`/admin_status` - Show bot status information.
`/admin_toggle_scan_notify <enable>` - Toggle API scan notifications.
`/admin_global_snipe <enable>` - Toggle global sniping mode.
`/admin_set_ram <megabytes>` - Allocate artificial memory (in MB).
`/admin_unset_ram` - Free the artificial memory.
`/admin_help` - Show this help message.
    """
    await interaction.response.send_message(help_message, ephemeral=True)
admin_help.dm_permission = True

#########################################
# Prefix Command for Debugging (Admin)  #
#########################################

@bot.command(name="admin_status")
async def admin_status_prefix(ctx):
    if str(ctx.author.id) != ADMIN_ID:
        await ctx.send("❌ You don't have permission to use this command.")
        return
    status_message = await get_admin_status_message()
    await ctx.send(status_message)

#########################################
# Bot Startup                           #
#########################################

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await initialize_storage()
    try:
        synced = await bot.tree.sync()
        print(f"🚀 Synced {len(synced)} slash command(s).")
    except Exception as e:
        print("🔥 Failed to sync commands:", e)
    asyncio.create_task(check_courses())
    print("🚀 Started monitoring courses!")

bot.run(TOKEN)
