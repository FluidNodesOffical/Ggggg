import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import sqlite3
from typing import Optional
import asyncio
import logging
import secrets
import string
import pylxd
from pylxd import Client

# Configure logging
logging.basicConfig(filename='bot.log', level=logging.INFO, 
                    format='%(asctime)s:%(levelname)s:%(message)s')

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID'))  # Your Discord server ID
ADMIN_ROLE_ID = int(os.getenv('ADMIN_ROLE_ID'))  # Your admin role ID
MAIN_VPS_IP = '138.245.6.206'  # Your main VPS IP

# Initialize bot with intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Initialize LXD client
lxd_client = Client()  # Connect to local LXD on the main VPS

# Database setup for tracking VPS instances
def init_db():
    conn = sqlite3.connect('vps_database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vps (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 user_id INTEGER,
                 vps_ip TEXT,
                 vps_name TEXT,
                 ram_mb INTEGER,
                 ssh_port INTEGER,
                 password TEXT,
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                 )''')
    conn.commit()
    conn.close()

# Generate secure random password
def generate_password(length=16):
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(characters) for _ in range(length))

# Get an available SSH port
def get_available_ssh_port():
    conn = sqlite3.connect('vps_database.db')
    c = conn.cursor()
    c.execute('SELECT ssh_port FROM vps WHERE ssh_port IS NOT NULL')
    used_ports = [row[0] for row in c.fetchall()]
    conn.close()
    for port in range(2222, 3000):  # Range of dynamic ports
        if port not in used_ports:
            return port
    return None

# Create container-based VPS using LXD
async def create_vps(user_id: int, ram_mb: int) -> Optional[dict]:
    try:
        vps_name = f'vps-{user_id}-{int(asyncio.get_event_loop().time())}'
        password = generate_password()
        ssh_port = get_available_ssh_port()
        if not ssh_port:
            logging.error(f"No available SSH ports for user {user_id}")
            return None

        # Create LXD container
        config = {
            'name': vps_name,
            'source': {'type': 'image', 'alias': 'ubuntu/20.04'},
            'config': {
                'limits.memory': f'{ram_mb}MB',
                'limits.cpu': '1'
            }
        }
        container = lxd_client.containers.create(config, wait=True)
        
        # Start container and configure SSH
        container.start(wait=True)
        container.execute([
            'bash', '-c',
            f'apt update && apt install -y openssh-server && '
            f'useradd -m -p $(openssl passwd -6 "{password}") vpsuser && '
            f'echo "Port {ssh_port}" >> /etc/ssh/sshd_config && '
            f'service ssh restart'
        ])
        
        # Set up port forwarding (host -> container)
        container.devices[f'proxy-ssh-{ssh_port}'] = {
            'type': 'proxy',
            'listen': f'tcp:{MAIN_VPS_IP}:{ssh_port}',
            'connect': f'tcp:127.0.0.1:22'
        }
        container.save(wait=True)
        
        logging.info(f"VPS created for user {user_id}: IP={MAIN_VPS_IP}, Name={vps_name}, SSH_Port={ssh_port}, Password={password}")
        return {'ip': MAIN_VPS_IP, 'name': vps_name, 'ssh_port': ssh_port, 'password': password}
    except Exception as e:
        logging.error(f"Error creating VPS for user {user_id}: {e}")
        return None

# Bot ready event
@bot.event
async def on_ready():
    logging.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="By PowerDev | /help"))
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        logging.info(f'Synced {len(synced)} command(s)')
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")
    init_db()

# Bot info command
@app_commands.command(name='botinfo', description='Shows bot information')
async def botinfo(interaction: discord.Interaction):
    embed = discord.Embed(title="PowerDev Bot", description="Made by PowerDev", color=0x00ff00)
    embed.add_field(name="Version", value="1.5.0", inline=True)
    embed.add_field(name="Status", value="Watching By PowerDev | /help", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Create VPS command
@app_commands.command(name='createvps', description='Create a new VPS instance')
@app_commands.describe(ram_mb='RAM in MB for the VPS')
async def createvps(interaction: discord.Interaction, ram_mb: int):
    await interaction.response.send_message("Creating VPS... (Processing)", ephemeral=True)
    user_id = interaction.user.id
    vps_data = await create_vps(user_id, ram_mb)
    
    if vps_data:
        conn = sqlite3.connect('vps_database.db')
        c = conn.cursor()
        c.execute('INSERT INTO vps (user_id, vps_ip, vps_name, ram_mb, ssh_port, password) VALUES (?, ?, ?, ?, ?, ?)',
                  (user_id, vps_data['ip'], vps_data['name'], ram_mb, vps_data['ssh_port'], vps_data['password']))
        conn.commit()
        conn.close()
        
        # Send details via DM
        user = interaction.user
        embed = discord.Embed(title="VPS Created Successfully!", color=0x00ff00)
        embed.add_field(name="VPS Name", value=vps_data['name'], inline=False)
        embed.add_field(name="IP Address", value=f"{vps_data['ip']}:{vps_data['ssh_port']}", inline=False)
        embed.add_field(name="RAM", value=f"{ram_mb} MB", inline=False)
        embed.add_field(name="SSH Port", value=vps_data['ssh_port'], inline=False)
        embed.add_field(name="Username", value="vpsuser", inline=False)
        embed.add_field(name="Password", value=vps_data['password'], inline=False)
        embed.set_footer(text="Store this password securely! Access via: ssh vpsuser@138.245.6.206 -p <port>")
        try:
            await user.send(embed=embed)
            await interaction.followup.send("VPS details sent to your DM!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("Failed to send DM. Please enable DMs from server members.", ephemeral=True)
    else:
        await interaction.followup.send("Failed to create VPS. Check logs or try again.", ephemeral=True)

# List all VPS (admin only)
@app_commands.command(name='listall', description='List all VPS instances (Admin only)')
async def listall(interaction: discord.Interaction):
    if ADMIN_ROLE_ID not in [role.id for role in interaction.user.roles]:
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    conn = sqlite3.connect('vps_database.db')
    c = conn.cursor()
    c.execute('SELECT user_id, vps_ip, vps_name, ram_mb, ssh_port, password, created_at FROM vps')
    vps_list = c.fetchall()
    conn.close()
    
    if not vps_list:
        await interaction.response.send_message("No VPS instances found.", ephemeral=True)
        return
    
    embed = discord.Embed(title="All VPS Instances", color=0x00ff00)
    for vps in vps_list:
        try:
            user = await bot.fetch_user(vps[0])
            username = user.name
        except:
            username = "Unknown User"
        embed.add_field(
            name=f"VPS: {vps[2]}",
            value=f"User: {username}\nIP: {vps[1]}:{vps[4]}\nRAM: {vps[3]} MB\nSSH Port: {vps[4]}\nPassword: [Hidden for security]\nCreated: {vps[6]}",
            inline=False
        )
    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("VPS list sent to your DM!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Failed to send DM. Please enable DMs.", ephemeral=True)

# List own VPS
@app_commands.command(name='listown', description='List your VPS instances')
async def listown(interaction: discord.Interaction):
    user_id = interaction.user.id
    conn = sqlite3.connect('vps_database.db')
    c = conn.cursor()
    c.execute('SELECT vps_ip, vps_name, ram_mb, ssh_port, password, created_at FROM vps WHERE user_id = ?', (user_id,))
    vps_list = c.fetchall()
    conn.close()
    
    if not vps_list:
        await interaction.response.send_message("You have no VPS instances.", ephemeral=True)
        return
    
    embed = discord.Embed(title="Your VPS Instances", color=0x00ff00)
    for vps in vps_list:
        embed.add_field(
            name=f"VPS: {vps[1]}",
            value=f"IP: {vps[0]}:{vps[3]}\nRAM: {vps[2]} MB\nSSH Port: {vps[3]}\nUsername: vpsuser\nPassword: {vps[4]}\nCreated: {vps[5]}",
            inline=False
        )
    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("Your VPS list sent to your DM!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Failed to send DM. Please enable DMs.", ephemeral=True)

# Help command
@app_commands.command(name='help', description='Show available commands')
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="PowerDev Bot Commands", description="Made by PowerDev", color=0x00ff00)
    embed.add_field(name="/botinfo", value="Shows bot information", inline=False)
    embed.add_field(name="/createvps <ram_mb>", value="Create a new VPS with specified RAM (in MB)", inline=False)
    embed.add_field(name="/listown", value="List your VPS instances", inline=False)
    if ADMIN_ROLE_ID in [role.id for role in interaction.user.roles]:
        embed.add_field(name="/listall", value="List all VPS instances (Admin only)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Run the bot
bot.run(TOKEN)
