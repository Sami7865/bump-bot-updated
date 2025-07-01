import discord
from discord.ext import tasks, commands
from discord import app_commands
import os
import asyncio
import datetime
from pymongo import MongoClient

# Get values from Render Environment Variables
TOKEN = os.environ.get("DISCORD_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True

client = commands.Bot(command_prefix="!", intents=intents)
tree = app_commands.CommandTree(client)

mongo = MongoClient(MONGO_URI)
db = mongo["bump_bot"]
bump_data = db["bump_data"]
config_data = db["config_data"]

REMINDER_INTERVAL = 2 * 60 * 60  # 2 hours

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

    if message.guild and message.content.lower().startswith("!d bump"):
        guild_id = str(message.guild.id)
        now = datetime.datetime.utcnow()
        user_id = str(message.author.id)

        entry = bump_data.find_one({"guild_id": guild_id})

        bump_data.update_one(
            {"guild_id": guild_id},
            {
                "$set": {
                    "user_id": user_id,
                    "last_bump": now
                }
            },
            upsert=True
        )

        config = config_data.find_one({"guild_id": guild_id})
        if config and "log_channel_id" in config:
            try:
                log_channel = message.guild.get_channel(int(config["log_channel_id"]))
                role_ping = f"<@&{config['role_id']}>" if "role_id" in config else ""
                await log_channel.send(f"📢 {message.author.mention} bumped the server! {role_ping}")
            except Exception as e:
                print("❌ Logging failed:", e)

        print(f"✅ Bump recorded by {message.author} at {now}")

    await client.process_commands(message)

@tasks.loop(seconds=60)
async def bump_reminder_loop():
    now = datetime.datetime.utcnow()
    for entry in bump_data.find():
        guild_id = entry.get("guild_id")
        user_id = entry.get("user_id")
        last_bump = entry.get("last_bump")

        if last_bump and user_id:
            elapsed = (now - last_bump).total_seconds()
            if elapsed >= REMINDER_INTERVAL:
                try:
                    user = await client.fetch_user(int(user_id))
                    await user.send("⏰ Hey! It's time to bump the server again with `!d bump`.")
                    bump_data.update_one(
                        {"guild_id": guild_id},
                        {"$set": {"last_bump": now}}
                    )
                except Exception as e:
                    print("❌ DM failed:", e)

### Slash Commands ###

@tree.command(name="bumpstatus", description="Check how long until your next bump reminder")
@app_commands.checks.has_permissions(manage_guild=True)
async def bumpstatus(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    data = bump_data.find_one({"guild_id": guild_id})
    if not data:
        await interaction.response.send_message("❌ No bump record found yet.", ephemeral=True)
        return

    last = data.get("last_bump")
    user_id = data.get("user_id")
    now = datetime.datetime.utcnow()

    if not last or not user_id:
        await interaction.response.send_message("❌ No valid bump data.", ephemeral=True)
        return

    remaining = max(0, REMINDER_INTERVAL - int((now - last).total_seconds()))
    minutes = remaining // 60
    await interaction.response.send_message(
        f"🕒 Next bump reminder for <@{user_id}> in **{minutes} minutes**", ephemeral=True
    )

@tree.command(name="setlogchannel", description="Set the log channel for bump notifications")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"log_channel_id": str(channel.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@tree.command(name="setpingrole", description="Set the role to ping on bumps")
@app_commands.checks.has_permissions(administrator=True)
async def setpingrole(interaction: discord.Interaction, role: discord.Role):
    config_data.update_one(
        {"guild_id": str(interaction.guild.id)},
        {"$set": {"role_id": str(role.id)}},
        upsert=True
    )
    await interaction.response.send_message(f"✅ Ping role set to {role.mention}", ephemeral=True)

### Error Handling ###
@setlogchannel.error
@setpingrole.error
async def permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("❌ You need administrator permission.", ephemeral=True)

client.run(TOKEN)
