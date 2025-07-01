import discord
from discord.ext import commands, tasks
from discord import app_commands
from pymongo import MongoClient
from keep_alive import keep_alive
from datetime import datetime, timedelta, UTC
import os

# Environment variables (set these in Render)
TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Bot setup
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

client = commands.Bot(command_prefix="!", intents=intents)
tree = client.tree

# MongoDB setup
mongo = MongoClient(MONGO_URI)
db = mongo["bump_bot"]
bump_data = db["bump_data"]
config_data = db["config_data"]
bump_history = db["bump_history"]

REMINDER_INTERVAL = 2 * 60 * 60  # 2 hours

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")

    # Set bot status: Listening to /bump
    activity = discord.Activity(type=discord.ActivityType.listening, name="/bump")
    await client.change_presence(status=discord.Status.online, activity=activity)

    bump_reminder_loop.start()

    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print("❌ Slash sync failed:", e)

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.content.lower().startswith("!d bump"):
        await handle_bump(message.author, message.guild)
        return

    if message.author.id == 302050872383242240 and message.embeds:
        embed = message.embeds[0]
        if embed.title and "bump done" in embed.title.lower():
            if message.interaction:
                bumper = message.interaction.user
                await handle_bump(bumper, message.guild)

    await client.process_commands(message)

async def handle_bump(user, guild):
    now = datetime.now(UTC)
    guild_id = str(guild.id)
    user_id = str(user.id)

    # Update latest bump
    bump_data.update_one(
        {"guild_id": guild_id},
        {"$set": {"user_id": user_id, "last_bump": now}},
        upsert=True
    )

    # Store history
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
            await log_channel.send(
                f"📢 {user.mention} bumped the server!\n"
                f"🕒 Next reminder at **{next_bump.strftime('%Y-%m-%d %H:%M:%S UTC')}**"
            )
        except Exception as e:
            print("❌ Error sending log:", e)

@tasks.loop(seconds=60)
async def bump_reminder_loop():
    now = datetime.now(UTC)
    for entry in bump_data.find():
        guild_id = entry.get("guild_id")
        user_id = entry.get("user_id")
        last_bump = entry.get("last_bump")

        if not (guild_id and user_id and last_bump):
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
                print(f"❌ Reminder error: {e}")

# Slash: /bumpstatus
@tree.command(name="bumpstatus", description="Check how long until the next bump reminder")
@app_commands.checks.has_permissions(manage_guild=True)
async def bumpstatus(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    data = bump_data.find_one({"guild_id": guild_id})
    if not data:
        await interaction.response.send_message("❌ No bumps recorded yet.", ephemeral=True)
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
@tree.command(name="setlogchannel", description="Set the log channel for bump tracking")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"log_channel_id": str(channel.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

# Slash: /setpingrole
@tree.command(name="setpingrole", description="Set the role to ping after 2 hours")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"role_id": str(role.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

# Slash: /bumphistory
@tree.command(name="bumphistory", description="See the last 10 bumps")
@app_commands.checks.has_permissions(manage_guild=True)
async def bumphistory(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    history = bump_history.find({"guild_id": guild_id}).sort("timestamp", -1).limit(10)
    
    lines = []
    for record in history:
        user_id = record["user_id"]
        timestamp = record["timestamp"].strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"<@{user_id}> — `{timestamp}`")

    if not lines:
        await interaction.response.send_message("📭 No bump history found.", ephemeral=True)
    else:
        await interaction.response.send_message("📜 **Recent Bumps:**\n" + "\n".join(lines), ephemeral=True)

# Slash: /userbumps
@tree.command(name="userbumps", description="Check how many times a user bumped")
@app_commands.checks.has_permissions(manage_guild=True)
async def userbumps(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)

    bumps = list(bump_history.find({
        "guild_id": guild_id,
        "user_id": user_id
    }).sort("timestamp", -1).limit(10))

    if not bumps:
        await interaction.response.send_message(f"❌ {user.mention} hasn't bumped yet.", ephemeral=True)
    else:
        lines = [f"`{b['timestamp'].strftime('%Y-%m-%d %H:%M UTC')}`" for b in bumps]
        total = bump_history.count_documents({
            "guild_id": guild_id,
            "user_id": user_id
        })
        await interaction.response.send_message(
            f"📈 {user.mention} has bumped **{total}** time(s).\n"
            f"🕒 Recent bumps:\n" + "\n".join(lines),
            ephemeral=True
        )

# Permissions error handler
@setlogchannel.error
@setpingrole.error
@bumpstatus.error
@bumphistory.error
@userbumps.error
async def permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ You need `Manage Server` permission.", ephemeral=True)

# Keep alive + run
keep_alive()
client.run(TOKEN)
