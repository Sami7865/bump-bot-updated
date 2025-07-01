import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
from keep_alive import keep_alive
from datetime import datetime, timedelta, UTC
import os

# Environment variables (set in Render)
TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Discord bot setup
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
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print("❌ Sync failed:", e)

@client.event
async def on_message(message):
    if message.author.bot:
        return

    # Test bump trigger
    if message.content.lower().startswith("!d bump"):
        await handle_bump(message.author, message.guild)
        return

    # Detect actual Disboard bump
    if message.author.id == 302050872383242240 and message.embeds:
        embed = message.embeds[0]
        if embed.title and "bump done" in embed.title.lower():
            async for m in message.channel.history(limit=10):
                if m.content == "/bump" and not m.author.bot:
                    await handle_bump(m.author, m.guild)
                    break

    await client.process_commands(message)

# Bump handler
async def handle_bump(user, guild):
    now = datetime.now(UTC)
    guild_id = str(guild.id)
    user_id = str(user.id)

    bump_data.update_one(
        {"guild_id": guild_id},
        {"$set": {"user_id": user_id, "last_bump": now}},
        upsert=True
    )

    bump_history.insert_one({
        "guild_id": guild_id,
        "user_id": user_id,
        "timestamp": now
    })

    config = config_data.find_one({"guild_id": guild_id})
    if config and "log_channel_id" in config:
        try:
            log_channel = guild.get_channel(int(config["log_channel_id"]))
            next_bump = now + timedelta(seconds=REMINDER_INTERVAL)
            next_str = next_bump.strftime("%Y-%m-%d %H:%M:%S UTC")

            await log_channel.send(
                f"📢 {user.mention} bumped the server!\n"
                f"🕒 Next reminder at **{next_str}**"
            )
        except Exception as e:
            print("❌ Log send failed:", e)

    print(f"✅ Bump recorded from {user}")

# Reminder loop
@tasks.loop(seconds=60)
async def bump_reminder_loop():
    now = datetime.now(UTC)
    for entry in bump_data.find():
        guild_id = entry.get("guild_id")
        user_id = entry.get("user_id")
        last_bump = entry.get("last_bump")

        if not (last_bump and user_id):
            continue

        if last_bump.tzinfo is None:
            last_bump = last_bump.replace(tzinfo=UTC)

        elapsed = (now - last_bump).total_seconds()
        if elapsed >= REMINDER_INTERVAL:
            try:
                guild = client.get_guild(int(guild_id))
                if not guild:
                    continue

                config = config_data.find_one({"guild_id": guild_id})
                if not config or "log_channel_id" not in config:
                    continue

                log_channel = guild.get_channel(int(config["log_channel_id"]))
                if not log_channel:
                    continue

                member = guild.get_member(int(user_id))
                role_mention = f"<@&{config['role_id']}>" if "role_id" in config else ""

                await log_channel.send(
                    f"🔁 It's time to bump again! {role_mention} — Last bump by {member.mention if member else f'<@{user_id}>'}"
                )

                bump_data.update_one(
                    {"guild_id": guild_id},
                    {"$set": {"last_bump": now}}
                )

            except Exception as e:
                print(f"❌ Reminder error in guild {guild_id}: {e}")

# Slash command: /bumpstatus
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

# Slash command: /setlogchannel
@tree.command(name="setlogchannel", description="Set the log channel for bump messages")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"log_channel_id": str(channel.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

# Slash command: /setpingrole
@tree.command(name="setpingrole", description="Set the role to ping after 2hr reminder")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"role_id": str(role.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

# Error handling for permission issues
@setlogchannel.error
@setpingrole.error
@bumpstatus.error
async def permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ You need `Manage Server` permission.", ephemeral=True)

# Run bot + Flask keep_alive server
keep_alive()
client.run(TOKEN)
