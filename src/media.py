"""Bounded, memory-only decoding. No media paths, shell, or network access."""
from io import BytesIO
import subprocess
import wave


class MediaFailure(Exception):
    pass


def voice_wav(data, max_seconds):
    if not data.startswith(b'OggS'):
        raise MediaFailure('Unsupported voice format')
    try:
        result = subprocess.run([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
            '-protocol_whitelist', 'pipe', '-f', 'ogg', '-i', 'pipe:0',
            '-map', '0:a:0', '-vn', '-t', str(max_seconds + 1), '-ac', '1', '-ar', '16000',
            '-f', 's16le', 'pipe:1'], input=data, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=20, check=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
        pcm = result.stdout
        seconds = len(pcm) / 32000
        if not pcm or len(pcm) % 2 or seconds > max_seconds:
            raise MediaFailure('Voice exceeds duration limit')
        output = BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(pcm)
        return output.getvalue(), seconds
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure('Voice conversion failed') from None
