"""Bounded media decoding. No shell, no network; ffmpeg reads only a pipe or one private temp file."""
from io import BytesIO
import os
import subprocess
import tempfile
import wave

RATE = 16000              # 16 kHz mono s16le
BYTES_PER_SECOND = RATE * 2
FFMPEG_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C'}
FRAME_SIZE = 320          # Each tiled frame is 320x320 (circles are square).


class MediaFailure(Exception):
    pass


def _ffmpeg(args, data=None, timeout=30):
    result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1', *args],
                            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                            check=True, env=FFMPEG_ENV)
    return result.stdout


def _pcm_seconds(pcm, max_seconds):
    if len(pcm) % 2:
        raise MediaFailure('Invalid audio')
    seconds = len(pcm) / BYTES_PER_SECOND
    if seconds > max_seconds:
        raise MediaFailure('Audio exceeds duration limit')
    return seconds


def to_wav(pcm):
    output = BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def split_pcm(pcm, max_chunk_seconds):
    """Pieces of at most max_chunk_seconds, cut at the quietest 100 ms in the last 3 s of each piece."""
    limit = int(max_chunk_seconds * BYTES_PER_SECOND) & ~1
    window = BYTES_PER_SECOND // 10
    pieces, start = [], 0
    while len(pcm) - start > limit:
        end = start + limit
        best, quietest = end, None
        search_from = max(start + window, end - 3 * BYTES_PER_SECOND)
        for offset in range(search_from, end - window + 1, window):
            chunk = pcm[offset:offset + window]
            # Mean absolute amplitude of little-endian 16-bit samples.
            energy = sum(abs(int.from_bytes(chunk[i:i + 2], 'little', signed=True))
                         for i in range(0, len(chunk), 32))
            if quietest is None or energy < quietest:
                best, quietest = offset + window // 2 & ~1, energy
        pieces.append(pcm[start:best])
        start = best
    if len(pcm) > start:
        pieces.append(pcm[start:])
    return pieces


def voice_pcm(data, max_seconds):
    """Telegram voice (OGG/Opus) → 16 kHz mono PCM and its measured duration."""
    if not data.startswith(b'OggS'):
        raise MediaFailure('Unsupported voice format')
    try:
        pcm = _ffmpeg(['-protocol_whitelist', 'pipe', '-f', 'ogg', '-i', 'pipe:0', '-map', '0:a:0', '-vn',
                       '-t', str(max_seconds + 1), '-ac', '1', '-ar', str(RATE), '-f', 's16le', 'pipe:1'],
                      data, timeout=20)
        if not pcm:
            raise MediaFailure('Empty voice')
        return pcm, _pcm_seconds(pcm, max_seconds)
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure('Voice conversion failed') from None


def voice_wav(data, max_seconds):
    """One WAV for the whole message (kept for providers that accept the full length)."""
    pcm, seconds = voice_pcm(data, max_seconds)
    return to_wav(pcm), seconds


def frame_count(seconds):
    """Length-dependent: 1 frame up to 15 s, 2 up to 40 s, 3 beyond."""
    return 1 if seconds <= 15 else 2 if seconds <= 40 else 3


def video_note(data, max_seconds, want_frames=True):
    """Telegram video circle (MP4) → (pcm or b'', seconds, tiled JPEG or None, frame count).

    MP4 metadata may sit at the end, so ffmpeg reads one private file in the in-memory /tmp.
    """
    if len(data) < 12 or data[4:8] != b'ftyp':
        raise MediaFailure('Unsupported video format')
    handle, path = tempfile.mkstemp(prefix='video-note-', suffix='.mp4', dir='/tmp')
    try:
        with os.fdopen(handle, 'wb') as file:
            file.write(data)
        os.chmod(path, 0o600)
        whitelist = ['-protocol_whitelist', 'file', '-f', 'mp4', '-i', path]
        duration = _probe_seconds(path)
        if duration <= 0 or duration > max_seconds + 1:
            raise MediaFailure('Video exceeds duration limit')
        try:
            pcm = _ffmpeg([*whitelist, '-map', '0:a:0', '-vn', '-t', str(max_seconds + 1), '-ac', '1',
                           '-ar', str(RATE), '-f', 's16le', 'pipe:1'])
        except subprocess.CalledProcessError:
            pcm = b''  # A silent circle has no audio stream.
        if pcm:
            _pcm_seconds(pcm, max_seconds)
        frames, image = 0, None
        if want_frames:
            frames = frame_count(duration)
            # Evenly spaced frames, tiled left to right in time order, as one JPEG.
            image = _ffmpeg([*whitelist, '-map', '0:v:0', '-an',
                             '-vf', f'fps={frames}/{max(duration, 0.1):.3f},scale={FRAME_SIZE}:{FRAME_SIZE}:'
                                    f'force_original_aspect_ratio=decrease,pad={FRAME_SIZE}:{FRAME_SIZE}:-1:-1,'
                                    f'tile={frames}x1',
                             '-frames:v', '1', '-q:v', '4', '-f', 'mjpeg', 'pipe:1'])
            if not image.startswith(b'\xff\xd8\xff'):
                image, frames = None, 0
        return pcm, duration, image, frames
    except MediaFailure:
        raise
    except Exception:
        raise MediaFailure('Video conversion failed') from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _probe_seconds(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries',
                             'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
                            env=FFMPEG_ENV)
    return float(result.stdout.decode().strip() or 0)
