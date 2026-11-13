import math
import subprocess
import wave
from pathlib import Path

import numpy as np

from aerodoppler.audio_engine import analyze_samples, decode_audio, ffmpeg_decode_command, sniff_media
from aerodoppler.physics import straight_doppler


def _tone(times, frequency):
    phase = 2 * math.pi * np.cumsum(frequency) / 16000.0
    return (0.2 * np.sin(phase)).astype(np.float32)


def test_known_doppler_fit_and_exports_metrics():
    time = np.linspace(0.0, 12.0, 12 * 16000, endpoint=False)
    frequency = straight_doppler(time, 180.0, 110.0, 500.0, 6.0)
    result = analyze_samples(_tone(time, frequency), 16000)
    assert result["status"] == "accepted"
    assert result["aircraft_identification"] is False
    fit = result["fit"]
    assert fit["r2"] > 0.9
    assert fit["rmse_hz"] < 5.0
    assert abs(fit["f0_hz"] - 180.0) / 180.0 < 0.08
    assert abs(fit["speed_mps"] - 110.0) / 110.0 < 0.25
    assert result["spectrogram_png_base64"]
    assert "time_s,frequency_hz" in result["ridge"]["time_s"].__class__.__name__ or result["ridge"]["duration_s"] > 4


def test_silence_and_noise_and_linear_chirp_are_rejected():
    silence = analyze_samples(np.zeros(16000 * 6, dtype=np.float32), 16000)
    assert silence["status"] == "rejected"
    assert "silencio" in silence["reasons"]
    noise = analyze_samples(np.random.default_rng(2).normal(0, 0.05, 16000 * 8).astype(np.float32), 16000)
    assert noise["status"] == "rejected"
    time = np.linspace(0.0, 10.0, 10 * 16000, endpoint=False)
    chirp = analyze_samples(_tone(time, 320.0 - 18.0 * time), 16000)
    assert chirp["status"] == "rejected"
    assert "no_se_distingue_de_un_descenso_lineal" in chirp["reasons"] or "r2_bajo" in chirp["reasons"] or "rmse_alto" in chirp["reasons"]


def test_ffmpeg_command_is_a_list_and_blocks_remote_protocols():
    command = ffmpeg_decode_command("ffmpeg", Path("/tmp/input.bin"), 16000, 90)
    assert isinstance(command, list)
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
    assert "shell" not in command


def test_wav_mp3_and_m4a_decode(tmp_path: Path):
    wav_path = tmp_path / "tone.wav"
    samples = (0.2 * np.sin(2 * math.pi * 220 * np.arange(16000 * 2) / 16000)).astype(np.float32)
    pcm = (samples * 32767).astype(np.int16)
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm.tobytes())
    blob = wav_path.read_bytes()
    assert sniff_media(blob) == "wav"
    decoded = decode_audio("ffmpeg", "ffprobe", wav_path, 16000, 90, 20)
    assert decoded.size > 16000
    mp3 = tmp_path / "tone.mp3"
    m4a = tmp_path / "tone.m4a"
    subprocess.run(["ffmpeg", "-y", "-i", str(wav_path), str(mp3)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-i", str(wav_path), "-c:a", "aac", str(m4a)], check=True, capture_output=True)
    assert sniff_media(mp3.read_bytes()) == "mp3"
    assert sniff_media(m4a.read_bytes()) == "mp4"
    assert decode_audio("ffmpeg", "ffprobe", mp3, 16000, 90, 20).size > 1000
    assert decode_audio("ffmpeg", "ffprobe", m4a, 16000, 90, 20).size > 1000
