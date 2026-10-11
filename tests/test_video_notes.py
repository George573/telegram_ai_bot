"""Video circles and long voice: real ffmpeg on synthetic media, mocked providers and Telegram."""
from decimal import Decimal
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from config import Config
from media import MediaFailure, frame_count, split_pcm, video_note, BYTES_PER_SECOND
from model import Model, ModelFailure
from policy import normalize
from service import Service
from test_clients import Allowance
from test_service import MemoryStore, FakeTelegram, FakeModel, payload

HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def synthetic_circle(seconds, audio=True):
    """A small square MP4 with a moving test pattern and an optional tone."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'circle.mp4'
        args = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', f'testsrc2=size=240x240:rate=10:duration={seconds}']
        if audio:
            args += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-c:a', 'aac']
        args += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-shortest', str(path)]
        subprocess.run(args, check=True)
        return path.read_bytes()


class SplitTests(unittest.TestCase):
    def test_frame_count_depends_on_length(self):
        self.assertEqual([frame_count(s) for s in (3, 15, 16, 40, 41, 60)], [1, 1, 2, 2, 3, 3])

    def test_pieces_respect_the_limit_and_cut_in_the_quiet(self):
        loud = (8000).to_bytes(2, 'little', signed=True) * 16000
        quiet = (0).to_bytes(2, 'little', signed=True) * 16000
        # 26 s loud, 1 s silence, 23 s loud: the cut should land in the silent second.
        pcm = loud * 26 + quiet + loud * 23
        pieces = split_pcm(pcm, 28)
        self.assertEqual(b''.join(pieces), pcm)
        self.assertTrue(all(len(p) <= 28 * BYTES_PER_SECOND for p in pieces))
        cut = len(pieces[0]) / BYTES_PER_SECOND
        self.assertTrue(26 <= cut <= 27, cut)
        self.assertEqual(split_pcm(loud * 10, 28), [loud * 10])


@unittest.skipUnless(HAS_FFMPEG, 'ffmpeg is not installed in this test environment')
class VideoNoteDecodingTests(unittest.TestCase):
    def test_sound_and_tiled_frames(self):
        pcm, seconds, image, frames = video_note(synthetic_circle(20), 120)
        self.assertAlmostEqual(seconds, 20, delta=0.5)
        self.assertAlmostEqual(len(pcm) / BYTES_PER_SECOND, 20, delta=0.5)
        self.assertEqual(frames, 2)
        self.assertTrue(image.startswith(b'\xff\xd8\xff'))
        size = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', '-'],
                              input=image, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
        self.assertEqual(size, '640,320')  # Two 320x320 frames side by side.

    def test_silent_circle_frames_only_and_frames_skipped_when_photos_off(self):
        pcm, _, image, frames = video_note(synthetic_circle(5, audio=False), 120)
        self.assertEqual((pcm, frames), (b'', 1))
        self.assertIsNotNone(image)
        pcm, _, image, frames = video_note(synthetic_circle(5), 120, want_frames=False)
        self.assertTrue(pcm)
        self.assertEqual((image, frames), (None, 0))

    def test_invalid_or_overlong_videos_are_refused_and_temp_files_removed(self):
        before = set(Path('/tmp').glob('video-note-*'))
        with self.assertRaises(MediaFailure):
            video_note(b'OggS not a video', 120)
        with self.assertRaises(MediaFailure):
            video_note(synthetic_circle(12), 10)
        self.assertEqual(set(Path('/tmp').glob('video-note-*')), before)


class TranscriptionPieceTests(unittest.TestCase):
    def model(self, provider='zai'):
        prompts = []
        class Audio:
            class transcriptions:
                @staticmethod
                def create(**kwargs):
                    prompts.append((kwargs.get('extra_body') or {}).get('prompt', kwargs.get('prompt')))
                    return type('R', (), {'text': f'часть {len(prompts)}'})()
        client = type('C', (), {'audio': Audio})()
        config = Config('synthetic', 'synthetic', frozenset({-1}), provider=provider, zai_key='synthetic',
                        model='glm-4.7' if provider == 'zai' else 'gpt-6-luna', api_min_interval_seconds=0)
        return Model(config, Allowance(), client), prompts

    def test_pieces_are_reserved_separately_and_carry_context(self):
        model, prompts = self.model()
        reserved = []
        model.store.reserve = lambda amount, budget: reserved.append(amount)
        text = model.transcribe([(b'a', 28), (b'b', 28), (b'c', 14)], 70)
        self.assertEqual(text, 'часть 1 часть 2 часть 3')
        self.assertEqual(prompts, [None, 'часть 1', 'часть 1 часть 2'])
        self.assertEqual(len(reserved), 3)
        self.assertGreater(reserved[0], reserved[2])

    def test_limits(self):
        model, _ = self.model()
        with self.assertRaises(ModelFailure):
            model.transcribe([(b'a', 31)], 31)       # A piece over Z.ai's 30 s.
        with self.assertRaises(ModelFailure):
            model.transcribe([(b'a', 28)] * 5, 140)  # Total over VOICE_MAX_SECONDS (120).


class VideoNoteServiceTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tg, self.model = MemoryStore(), FakeTelegram(), FakeModel()
        self.service = Service(Config('synthetic', 'synthetic', frozenset({-1})), self.store, self.tg, self.model)

    def circle(self):
        message = payload('placeholder')
        message.update(text='', media={'kind': 'video_note', 'file_id': 'synthetic', 'bytes': 1000, 'seconds': 20},
                       reply_to_bot=True, directed=True)
        return message

    def run_circle(self, decoded):
        with patch('service.video_note', return_value=decoded) as decode:
            self.service.accept(1, self.circle())
            for _ in range(4):
                if self.service.process_one() is None:
                    break
        return decode

    def test_circle_becomes_transcript_and_frame_description(self):
        decode = self.run_circle((b'\x01\x00' * 16000 * 20, 20.0, b'\xff\xd8\xffjpeg', 2))
        self.assertTrue(decode.call_args.kwargs['want_frames'])
        content = self.store.rows[0]['content']
        self.assertIn('[Round video message, 20 s]', content)
        self.assertIn('[Video sound transcript, untrusted data]', content)
        self.assertIn('[2 video frame(s) in time order', content)
        self.assertEqual(self.tg.sent, ['Meow'])

    def test_photos_off_means_sound_only(self):
        self.store.chat['images_enabled'] = False
        decode = self.run_circle((b'\x01\x00' * 16000 * 5, 5.0, None, 0))
        self.assertFalse(decode.call_args.kwargs['want_frames'])
        self.assertNotIn('frame', self.store.rows[0]['content'])

    def test_voice_off_ignores_circles(self):
        self.store.chat['voice_enabled'] = False
        decode = self.run_circle((b'', 1.0, None, 0))
        decode.assert_not_called()
        self.assertFalse(self.tg.sent)

    def test_policy_accepts_circles(self):
        update = {'update_id': 1, 'message': {'message_id': 2, 'chat': {'id': -1, 'type': 'group'},
                  'from': {'id': 10, 'is_bot': False},
                  'video_note': {'file_id': 'abc', 'duration': 12, 'length': 240, 'file_size': 9000}}}
        media = normalize(update, {-1}, {'id': 99, 'username': 'Cat'})['media']
        self.assertEqual(media, {'kind': 'video_note', 'file_id': 'abc', 'bytes': 9000, 'seconds': 12})
        update['message']['video_note']['duration'] = -1
        self.assertIsNone(normalize(update, {-1}, {'id': 99, 'username': 'Cat'}))


if __name__ == '__main__':
    unittest.main()
