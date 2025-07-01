import discord
from discord.ext import tasks, commands
from discord import app_commands
import os
import asyncio
from datetime import datetime, timedelta, UTC
from pymongo import MongoClient
from keep_alive import keep_alive

# ENV variables
TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Setup bot
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree

mongo = MongoClient(MONGO_URI)
db = mongo["bump_bot"]
bump_data = db["bump_data"]
config_data = db["config_data"]
bump_history = db["bump_history"]

REMINDER_INTERVAL = 2 * 60 * 60  # 2 hours in seconds

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    bump_reminder_loop.start()
    try:
        synced = await client.tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print("❌ Sync failed:", e)

@client.event
async def on_message(message):
    if message.author.bot:
        return

    # Handle test !d bump command
    if message.content.lower().startswith("!d bump"):
        await handle_bump(message.author, message.guild)
        return  # prevent CommandNotFound

    # Handle actual Disboard bump embed
    if message.author.id == 302050872383242240 and message.embeds:
        embed = message.embeds[0]
        if embed.title and "bump done" in embed.title.lower():
            async for m in message.channel.history(limit=10):
                if m.content == "/bump" and not m.author.bot:
                    await handle_bump(m.author, m.guild)
                    break

    await client.process_commands(message)

# Handle bump event
async def handle_bump(user, guild):
    now = datetime.now(UTC)
    guild_id = str(guild.id)
    user_id = str(user.id)

    # Update last bump
    bump_data.update_one(
        {"guild_id": guild_id},
        {"$set": {"user_id": user_id, "last_bump": now}},
        upsert=True
    )

    # Store bump history
    bump_history.insert_one({
        "guild_id": guild_id,
        "user_id": user_id,
        "timestamp": now
    })

    # Log to channel
    config = config_data.find_one({"guild_id": guild_id})
    if config and "log_channel_id" in config:
        try:
            log_channel = guild.get_channel(int(config["log_channel_id"]))
            role_ping = f"<@&{config['role_id']}>" if "role_id" in config else ""
            next_bump = now + timedelta(seconds=REMINDER_INTERVAL)
            next_str = next_bump.strftime("%Y-%m-%d %H:%M:%S UTC")

            await log_channel.send(
                f"📢 {user.mention} bumped the server! {role_ping}\n"
                f"🕒 Next reminder at **{next_str}**"
            )
        except Exception as e:
            print("❌ Log error:", e)

    print(f"✅ Bump recorded from {user} at {now}")

# Background reminder loop
@tasks.loop(seconds=60)
async def bump_reminder_loop():
    now = datetime.now(UTC)
    for entry in bump_data.find():
        guild_id = entry.get("guild_id")
        user_id = entry.get("user_id")
        last_bump = entry.get("last_bump")

        if last_bump and user_id:
            if last_bump.tzinfo is None:
                last_bump = last_bump.replace(tzinfo=UTC)

            elapsed = (now - last_bump).total_seconds()
            if elapsed >= REMINDER_INTERVAL:
                try:
                    user = await client.fetch_user(int(user_id))
                    await user.send("⏰ Hey! It's time to bump the server again with `/bump`.")
                    bump_data.update_one(
                        {"guild_id": guild_id},
                        {"$set": {"last_bump": now}}
                    )
                except Exception as e:
                    print("❌ DM failed:", e)

# Slash: /bumpstatus
@tree.command(name="bumpstatus", description="Check time left until next bump reminder")
@app_commands.checks.has_permissions(manage_guild=True)
async def bumpstatus(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    data = bump_data.find_one({"guild_id": guild_id})
    if not data:
        await interaction.response.send_message("❌ No bump record yet.", ephemeral=True)
        return

    now = datetime.now(UTC)
    last = data.get("last_bump")
    user_id = data.get("user_id")

    if last is None or user_id is None:
        await interaction.response.send_message("❌ No valid bump data found.", ephemeral=True)
        return

    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)

    remaining = max(0, REMINDER_INTERVAL - int((now - last).total_seconds()))
    minutes = remaining // 60
    await interaction.response.send_message(
        f"⏱ Next reminder for <@{user_id}> in **{minutes} minutes**", ephemeral=True
    )

# Slash: /setlogchannel
@tree.command(name="setlogchannel", description="Set the log channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"log_channel_id": str(channel.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

# Slash: /setpingrole
@tree.command(name="setpingrole", description="Set the role to ping on bump")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"role_id": str(role.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

# Error handlers
@setlogchannel.error
@setpingrole.error
@bumpstatus.error
async def perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ You need admin permission.", ephemeral=True)

# Run server + bot
keep_alive()
client.run(TOKEN)
