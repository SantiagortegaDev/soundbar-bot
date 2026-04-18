import discord
from discord import app_commands
import aiohttp
import os
import asyncio
import tempfile

# ================= CONFIGURACIÓN =================
TOKEN = ""  # ← Reemplaza con tu token
# =================================================

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Estado en memoria ──────────────────────────────────────────────────────────
# { guild_id: [ {"name": str, "sound_id": str}, ... ] }
guild_sounds: dict[int, list[dict]] = {}

# { guild_id: discord.VoiceClient }
voice_clients: dict[int, discord.VoiceClient] = {}

# { guild_id: float }  (volumen entre 0.0 y 2.0, default 1.0)
guild_volume: dict[int, float] = {}
# ──────────────────────────────────────────────────────────────────────────────


def get_volume(guild_id: int) -> float:
    return guild_volume.get(guild_id, 1.0)


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    """
    Conecta el bot al canal de voz del usuario si no está ya conectado.
    Retorna el VoiceClient activo, o None si el usuario no está en un canal.
    """
    guild = interaction.guild
    member = guild.get_member(interaction.user.id)

    if member is None or member.voice is None or member.voice.channel is None:
        await interaction.followup.send(
            "❌ **No estás en ningún canal de voz.**\n"
            "Únete a un canal de voz primero y vuelve a intentarlo.",
            ephemeral=True
        )
        return None

    target_channel = member.voice.channel
    existing_vc = voice_clients.get(guild.id)

    # Ya está en el canal correcto
    if existing_vc and existing_vc.is_connected() and existing_vc.channel == target_channel:
        return existing_vc

    # Está en otro canal → moverse
    if existing_vc and existing_vc.is_connected():
        await existing_vc.move_to(target_channel)
        return existing_vc

    # No está conectado → conectarse
    try:
        vc = await target_channel.connect()
        voice_clients[guild.id] = vc
        return vc
    except discord.ClientException as e:
        await interaction.followup.send(
            f"❌ **No pude conectarme al canal** `{target_channel.name}`.\n"
            f"Asegúrate de que el bot tiene permisos para unirse a ese canal.\n`{e}`",
            ephemeral=True
        )
        return None
    except Exception as e:
        await interaction.followup.send(
            f"❌ **Error inesperado al conectarse al canal de voz.**\n`{e}`",
            ephemeral=True
        )
        return None


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ {client.user} conectado.")
    print("📋 Comandos activos: /refresh /play /stop /volume /disconnect")


# ──────────────────────────── /refresh ────────────────────────────────────────
@tree.command(
    name="refresh",
    description="Escanea los sonidos de la soundboard del servidor y los guarda en memoria"
)
@app_commands.guild_only()
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://discord.com/api/v10/guilds/{guild.id}/soundboard-sounds",
            headers={"Authorization": f"Bot {TOKEN}"}
        ) as resp:
            if resp.status == 403:
                await interaction.followup.send(
                    "❌ **Sin permiso para acceder a la soundboard.**\n"
                    "Asegúrate de que el bot tiene el permiso `Manage Guild` o acceso al canal.",
                    ephemeral=True
                )
                return
            if resp.status != 200:
                await interaction.followup.send(
                    f"❌ **Error al obtener la soundboard** (código HTTP `{resp.status}`).\n"
                    "Inténtalo de nuevo en unos momentos.",
                    ephemeral=True
                )
                return

            data = await resp.json()
            sounds = data.get("items", [])

    if not sounds:
        await interaction.followup.send(
            "⚠️ **Este servidor no tiene sonidos en la soundboard.**\n"
            "Agrega sonidos desde Configuración del Servidor → Soundboard.",
            ephemeral=True
        )
        return

    guild_sounds[guild.id] = [
        {"name": s["name"], "sound_id": str(s["sound_id"])}
        for s in sounds
    ]

    lista = "\n".join(f"🔊 `{s['name']}`" for s in guild_sounds[guild.id])
    await interaction.followup.send(
        f"✅ **{len(sounds)} sonidos escaneados** en **{guild.name}**:\n\n{lista}\n\n"
        f"Usa `/play` para reproducir cualquiera de ellos.",
        ephemeral=True
    )


# ──────────────────────────── /play ───────────────────────────────────────────
@tree.command(
    name="play",
    description="Reproduce un sonido de la soundboard en tu canal de voz actual"
)
@app_commands.guild_only()
@app_commands.describe(sonido="Nombre del sonido (usa Tab para ver sugerencias)")
async def play(interaction: discord.Interaction, sonido: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    # ── Verificar que hay sonidos escaneados ──
    sounds = guild_sounds.get(guild.id)
    if not sounds:
        await interaction.followup.send(
            "❌ **No hay sonidos escaneados todavía.**\n"
            "Ejecuta `/refresh` primero para cargar los sonidos del servidor.",
            ephemeral=True
        )
        return

    # ── Buscar el sonido ──
    match = next((s for s in sounds if s["name"].lower() == sonido.lower()), None)
    if match is None:
        match = next((s for s in sounds if sonido.lower() in s["name"].lower()), None)
    if match is None:
        sugerencias = [s["name"] for s in sounds if sonido.lower()[:3] in s["name"].lower()][:5]
        hint = ""
        if sugerencias:
            hint = "\n💡 ¿Quisiste decir? " + ", ".join(f"`{s}`" for s in sugerencias)
        await interaction.followup.send(
            f"❌ **No encontré el sonido** `{sonido}`.\n"
            f"Usa `/refresh` para actualizar la lista y escribe el nombre exacto.{hint}",
            ephemeral=True
        )
        return

    # ── Auto-join al canal del usuario ──
    vc = await ensure_voice(interaction)
    if vc is None:
        return

    # ── Descargar y reproducir ──
    audio_url = f"https://cdn.discordapp.com/soundboard-sounds/{match['sound_id']}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(audio_url) as resp:
                if resp.status == 404:
                    await interaction.followup.send(
                        f"❌ **El audio `{match['name']}` ya no existe en Discord.**\n"
                        "Ejecuta `/refresh` para actualizar la lista de sonidos.",
                        ephemeral=True
                    )
                    return
                if resp.status != 200:
                    await interaction.followup.send(
                        f"❌ **No pude descargar el audio** (HTTP `{resp.status}`).\n"
                        "Inténtalo de nuevo en unos momentos.",
                        ephemeral=True
                    )
                    return
                audio_data = await resp.read()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name

        if vc.is_playing():
            vc.stop()

        volume = get_volume(guild.id)

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(tmp_path),
            volume=volume
        )

        def after_play(error):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            if error:
                print(f"[play] Error reproduciendo '{match['name']}': {error}")

        vc.play(source, after=after_play)

        vol_percent = int(volume * 100)
        await interaction.followup.send(
            f"▶️ Reproduciendo **{match['name']}** en **{vc.channel.name}** — 🔊 Volumen: `{vol_percent}%`",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ **Error inesperado al reproducir.**\n`{e}`",
            ephemeral=True
        )


@play.autocomplete("sonido")
async def play_sonido_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    guild = interaction.guild
    if not guild:
        return []
    sounds = guild_sounds.get(guild.id, [])
    return [
        app_commands.Choice(name=s["name"], value=s["name"])
        for s in sounds
        if current.lower() in s["name"].lower()
    ][:25]


# ──────────────────────────── /stop ───────────────────────────────────────────
@tree.command(
    name="stop",
    description="Detiene el audio que se está reproduciendo actualmente"
)
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    guild = interaction.guild
    vc = voice_clients.get(guild.id)

    if vc and vc.is_connected() and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏹️ Audio detenido.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "ℹ️ No hay ningún audio reproduciéndose en este momento.",
            ephemeral=True
        )


# ──────────────────────────── /volume ─────────────────────────────────────────
@tree.command(
    name="volume",
    description="Ajusta el volumen de reproducción (0 a 200)"
)
@app_commands.guild_only()
@app_commands.describe(nivel="Volumen entre 0 y 200 (100 = normal, 200 = máximo)")
async def volume(interaction: discord.Interaction, nivel: int):
    if nivel < 0 or nivel > 200:
        await interaction.response.send_message(
            "❌ **Valor inválido.** El volumen debe estar entre `0` y `200`.\n"
            "Ejemplos: `50` = mitad, `100` = normal, `200` = máximo.",
            ephemeral=True
        )
        return

    guild = interaction.guild
    guild_volume[guild.id] = nivel / 100.0

    # Ajustar volumen en tiempo real si hay audio reproduciéndose
    vc = voice_clients.get(guild.id)
    if vc and vc.is_playing() and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = nivel / 100.0

    emoji = "🔇" if nivel == 0 else "🔉" if nivel < 60 else "🔊"
    await interaction.response.send_message(
        f"{emoji} Volumen ajustado a `{nivel}%`.\n"
        f"Se aplicará a todos los sonidos reproducidos desde ahora.",
        ephemeral=True
    )


# ──────────────────────────── /disconnect ─────────────────────────────────────
@tree.command(
    name="disconnect",
    description="Desconecta el bot del canal de voz"
)
@app_commands.guild_only()
async def disconnect(interaction: discord.Interaction):
    guild = interaction.guild
    vc = voice_clients.get(guild.id)

    if vc and vc.is_connected():
        channel_name = vc.channel.name
        if vc.is_playing():
            vc.stop()
        await vc.disconnect()
        voice_clients.pop(guild.id, None)
        await interaction.response.send_message(
            f"👋 Desconectado de **{channel_name}**.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "ℹ️ El bot no está en ningún canal de voz actualmente.",
            ephemeral=True
        )


# ──────────────────────────── Limpieza al desconectarse ───────────────────────
@client.event
async def on_voice_state_update(member, before, after):
    """Si el bot se queda solo en el canal, se desconecta automáticamente."""
    if member.id == client.user.id:
        return

    guild = member.guild
    vc = voice_clients.get(guild.id)
    if vc and vc.is_connected():
        # Contar miembros humanos en el canal (sin contar al bot)
        human_members = [m for m in vc.channel.members if not m.bot]
        if len(human_members) == 0:
            if vc.is_playing():
                vc.stop()
            await vc.disconnect()
            voice_clients.pop(guild.id, None)
            print(f"[auto-disconnect] Me quedé solo en '{vc.channel.name}' → desconectado.")


# ================= EJECUCIÓN =================
if __name__ == "__main__":
    client.run(TOKEN)
