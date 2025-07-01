import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import pymongo
from pymongo import MongoClient
from flask import Flask
import threading

TOKEN = "your-bot-token"
MONGO_URI = "your-mongo-uri"
REMINDER_INTERVAL = 7200  # 2 hours in seconds

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True

client = commands.Bot(command_prefix="!", intents=intents)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["bumpbot"]
settings_col = db["settings"]
bumps_col = db["bumps"]

last_bump_data = {}

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    activity = discord.Activity(type=discord.ActivityType.listening, name="/bump")
    await client.change_presence(status=discord.Status.online, activity=activity)
    await client.tree.sync()
    bump_reminder_loop.start()

@client.event
async def on_message(message):
    if message.author.id == 302050872383242240 and message.embeds:
        embed = message.embeds[0]
        if embed.title and "DISBOARD" in embed.title and "Bump done!" in embed.description:
            bumper = None
            if message.interaction:
                bumper = message.interaction.user
            else:
                async for msg in message.channel.history(limit=5, before=message):
                    if msg.type == discord.MessageType.application_command and "/bump" in msg.content.lower():
                        bumper = msg.author
                        break

            if not bumper:
                print("⚠️ Bumper not found.")
                return

            guild_id = str(message.guild.id)
            now = datetime.datetime.now(datetime.timezone.utc)
            last_bump_data[guild_id] = {"time": now, "user_id": bumper.id}

            bumps_col.insert_one({
                "guild_id": guild_id,
                "user_id": bumper.id,
                "timestamp": now
            })

            settings = settings_col.find_one({"guild_id": guild_id}) or {}
            log_channel_id = settings.get("log_channel_id")

            if log_channel_id:
                log_channel = client.get_channel(log_channel_id)
                if log_channel:
                    embed_log = discord.Embed(
                        title="📢 Bump Tracked!",
                        description=f"User {bumper.mention} bumped the server!",
                        color=discord.Color.green()
                    )
                    embed_log.add_field(
                        name="Next Reminder",
                        value=f"<t:{int(now.timestamp()) + REMINDER_INTERVAL}:R>"
                    )
                    await log_channel.send(embed=embed_log)

@tasks.loop(seconds=60)
async def bump_reminder_loop():
    now = datetime.datetime.now(datetime.timezone.utc)
    for guild_id, data in last_bump_data.items():
        last_bump = data["time"]
        bumper_id = data["user_id"]
        elapsed = (now - last_bump).total_seconds()
        if elapsed >= REMINDER_INTERVAL:
            settings = settings_col.find_one({"guild_id": guild_id}) or {}
            log_channel_id = settings.get("log_channel_id")
            role_id = settings.get("ping_role_id")
            if log_channel_id and role_id:
                log_channel = client.get_channel(log_channel_id)
                if log_channel:
                    user = await client.fetch_user(bumper_id)
                    await log_channel.send(
                        f"🔁 It's time to bump again! <@&{role_id}> — Last bump by {user.mention}"
                    )
            last_bump_data[guild_id] = {"time": now, "user_id": bumper_id}

# === SLASH COMMANDS ===

@client.tree.command(name="setlogchannel", description="Set the channel where bump logs will be sent")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction):
    settings_col.update_one(
        {"guild_id": str(interaction.guild_id)},
        {"$set": {"log_channel_id": interaction.channel_id}},
        upsert=True
    )
    await interaction.response.send_message("✅ This channel has been set for bump logs.", ephemeral=True)

@client.tree.command(name="setpingrole", description="Set the role to be pinged for bump reminders")
@app_commands.describe(role="Role to ping after 2 hours")
@app_commands.checks.has_permissions(administrator=True)
async def set_ping_role(interaction: discord.Interaction, role: discord.Role):
    settings_col.update_one(
        {"guild_id": str(interaction.guild_id)},
        {"$set": {"ping_role_id": role.id}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ {role.mention} will be pinged for bump reminders.", ephemeral=True)

@client.tree.command(name="bumpstatus", description="Check time left until next bump")
async def bumpstatus(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    data = last_bump_data.get(guild_id)
    if not data:
        await interaction.response.send_message("❌ No bumps recorded yet.", ephemeral=True)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    remaining = max(0, REMINDER_INTERVAL - int((now - data["time"]).total_seconds()))
    next_time = now + datetime.timedelta(seconds=remaining)
    await interaction.response.send_message(
        f"🕒 Next bump reminder <t:{int(next_time.timestamp())}:R> (at <t:{int(next_time.timestamp())}:t>)",
        ephemeral=True
    )

@client.tree.command(name="bumphistory", description="Show recent bump history")
async def bumphistory(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    records = bumps_col.find({"guild_id": guild_id}).sort("timestamp", -1).limit(5)
    lines = []
    for r in records:
        user = await client.fetch_user(r["user_id"])
        ts = r["timestamp"].replace(tzinfo=datetime.timezone.utc)
        lines.append(f"🔹 {user.mention} — <t:{int(ts.timestamp())}:R>")

    if not lines:
        await interaction.response.send_message("📭 No bump history found.", ephemeral=True)
    else:
        await interaction.response.send_message("📜 **Recent Bumps:**\n" + "\n".join(lines), ephemeral=True)

@client.tree.command(name="userbumps", description="Check how many times a user has bumped")
@app_commands.describe(user="The user to check")
async def userbumps(interaction: discord.Interaction, user: discord.User):
    guild_id = str(interaction.guild_id)
    count = bumps_col.count_documents({"guild_id": guild_id, "user_id": user.id})
    await interaction.response.send_message(f"📊 {user.mention} has bumped **{count}** time(s).", ephemeral=True)

# === Keep-alive for Render ===
app = Flask("")

@app.route("/")
def home():
    return "Bump bot is running!"

def keep_alive():
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

keep_alive()
client.run(TOKEN)
