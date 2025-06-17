# =============================================================
# 🎬 Gerçek Zamanlı Video Sansür + Bip Overlay – TAM KOD
# =============================================================
#  Bu sürüm, Whisper‐tabanlı kelime zaman damgalarını kullanarak
#  küfürlü kelimelerin tam üstüne 1 kHz’lik bip yerleştirir.
#  İstemeden TTS ile tekrar okuma yerine orijinal ses korunur.
# -------------------------------------------------------------

import streamlit as st
import tempfile
import os
import whisper
import torch
import numpy as np
import re
import threading
import queue
import time
from pydub import AudioSegment, generators
from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline
import scipy.signal
from moviepy.editor import VideoFileClip, AudioFileClip
import base64
from io import BytesIO

# 🔧 Global Ayarlar
WHISPER_SR = 16000          # Whisper giriş örnekleme hızı
THRESHOLD = 0.90            # Profanity eşiği
CHUNK_DURATION = 5          # Analiz parçası uzunluğu (sn)
BEEP_FREQ = 1000            # Bip frekansı (Hz)
BEEP_GAIN = -6              # Bip ses seviyesi (dB)
CUSTOM_PROFANE = {
    "fenasik", "kerilim", "pompa", "fenasik kerilim", "donaltıp"
}

# İş kuyrukları
processing_queue = queue.Queue()
results_queue = queue.Queue()
is_processing = False

# -------------------------------------------------------------
#  Model Yükleme
# -------------------------------------------------------------
@st.cache_resource(show_spinner=True)
def load_models():
    whisper_model = whisper.load_model("small")
    model_name = "Overfit-GM/distilbert-base-turkish-cased-offensive"
    device = 0 if torch.cuda.is_available() else -1
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    clf_model = AutoModelForSequenceClassification.from_pretrained(model_name)
    offense_detector = pipeline(
        "text-classification",
        model=clf_model,
        tokenizer=tokenizer,
        device=device,
        return_all_scores=True,
    )
    return whisper_model, offense_detector

# -------------------------------------------------------------
#  Profanity Kontrol Yardımcıları
# -------------------------------------------------------------

def is_profane(word: str, offense_detector) -> bool:
    if word.lower() in CUSTOM_PROFANE:
        return True
    try:
        scores = offense_detector(word)[0]
        return next(s["score"] for s in scores if s["label"] == "PROFANITY") >= THRESHOLD
    except Exception:
        return False

# -------------------------------------------------------------
#  Ses Üzerine Bip Örtme
# -------------------------------------------------------------

def beep_over_intervals(audio_seg: AudioSegment, intervals_ms):
    """Belirtilen milisaniye aralıklarında orijinal sesi sessizleştirir ve sadece bip ekler."""
    output = audio_seg[:]  # kopya
    for start_ms, end_ms in intervals_ms:
        duration_ms = max(0, int(end_ms - start_ms))
        if duration_ms <= 0:
            continue
        
        # Bip sesi oluştur
        beep = generators.Sine(BEEP_FREQ).to_audio_segment(duration=duration_ms).apply_gain(BEEP_GAIN)
        
        # İlgili aralığı sessizleştir
        before = output[:int(start_ms)]
        after = output[int(end_ms):]
        silence = AudioSegment.silent(duration=duration_ms)
        
        # Sessizlik + bip overlay
        censored_part = silence.overlay(beep)
        
        # Parçaları birleştir
        output = before + censored_part + after
    
    return output


# -------------------------------------------------------------
#  Video’dan Ses Parçalarını Çek
# -------------------------------------------------------------

def extract_audio_chunks_from_video(video_path):
    """Video’dan ses çıkarır ve CHUNK_DURATION uzunluklu WAV parçaları üretir."""
    try:
        video = VideoFileClip(video_path)
        audio = video.audio
        duration = video.duration
        current = 0
        while current < duration:
            end = min(current + CHUNK_DURATION, duration)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                audio.subclip(current, end).write_audiofile(tmp.name, verbose=False, logger=None)
                yield {
                    "audio_path": tmp.name,
                    "chunk_id": int(current / CHUNK_DURATION),
                    "start_time": current,
                    "end_time": end,
                }
            current = end
        audio.close(); video.close()
    except Exception as e:
        st.error(f"Video parçalara ayrılırken hata: {e}")
        return None

# -------------------------------------------------------------
#  Tek Parça İşleme (+ Bip Ekleme)
# -------------------------------------------------------------

def process_audio_chunk(chunk_data, whisper_model, offense_detector):
    """WAV parçasını transcribe eder, küfürleri bip’ler, sansürlü segmenti döndürür."""
    path = chunk_data["audio_path"]

    try:
        # ---------- 1) WAV → NumPy ----------
        segment = AudioSegment.from_wav(path)
        samples = np.array(segment.get_array_of_samples()).astype(np.float32)

        if segment.channels == 2:                      # mono ya dön
            samples = samples.reshape((-1, 2)).mean(axis=1)
        samples /= 32768.0                             # int16 → float32 [-1,1]

        if segment.frame_rate != WHISPER_SR:           # 44 kHz vb. ise 16 kHz e yeniden örnekle
            samples = scipy.signal.resample_poly(
                samples, WHISPER_SR, segment.frame_rate
            ).astype(np.float32)

        # ---------- 2) Word-timestamp’lı Whisper ----------
        result = whisper_model.transcribe(
            samples, language="tr", word_timestamps=True
        )

        words = []  # (kelime, mutlak-start-ms, mutlak-end-ms)
        for seg in result["segments"]:
            for w in seg["words"]:
                start_ms = (w["start"] + chunk_data["start_time"]) * 1000
                end_ms   = (w["end"]   + chunk_data["start_time"]) * 1000
                words.append((w["word"].strip(), start_ms, end_ms))

        # ---------- 3) Profanity zaman damgaları ----------
        profanity_intervals = [
            (s, e) for w, s, e in words if is_profane(w, offense_detector)
        ]

        # ---------- 4) Bip ekle + orijinal sesi sustur ----------
        censored_seg = beep_over_intervals(
            segment,
            [
                (s - chunk_data["start_time"] * 1000,
                 e - chunk_data["start_time"] * 1000)
                for s, e in profanity_intervals
            ]
        )

        # ---------- 5) Sansürlü metin oluştur 🔄 ----------
        masked_tokens = []
        for w, _, _ in words:
            masked_tokens.append("*" * len(w) if is_profane(w, offense_detector) else w)
        masked_text = " ".join(masked_tokens)

        # ---------- 6) WAV’i kaydet ----------
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            censored_seg.export(tmp_out.name, format="wav")
            censored_path = tmp_out.name

        # ---------- 7) Temizlik & çıktı ----------
        os.unlink(path)
        return {
            **chunk_data,
            "text":        result["text"].strip(),       # orijinal tam metin
            "masked_text": masked_text,                  # ***’li sürüm 🔄
            "bad_words":   {w for w, _, _ in words if is_profane(w, offense_detector)},
            "censored_audio_path": censored_path,
        }

    except Exception as e:
        if os.path.exists(path):
            os.unlink(path)
        return {
            **chunk_data,
            "text": f"[Hata: {e}]",
            "bad_words": set(),
            "masked_text": "",
            "censored_audio_path": None,
        }


# -------------------------------------------------------------
#  Arka Plan Worker
# -------------------------------------------------------------

def audio_processor_worker():
    global is_processing
    whisper_model, offense_detector = load_models()
    while is_processing:
        try:
            chunk_data = processing_queue.get(timeout=1)
            if chunk_data is None:
                break
            res = process_audio_chunk(chunk_data, whisper_model, offense_detector)
            results_queue.put(res)
        except queue.Empty:
            continue
        except Exception as e:
            st.error(f"Worker hatası: {e}")

# -------------------------------------------------------------
#  Sonunda Tüm Sansürlü Parçaları Birleştir
# -------------------------------------------------------------

def combine_audio_segments(paths):
    combined = AudioSegment.empty()
    for p in paths:
        if p and os.path.exists(p):
            combined += AudioSegment.from_wav(p)
            os.unlink(p)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        combined.export(tmp.name, format="wav")
        return tmp.name

# -------------------------------------------------------------
#  Streamlit Arayüzü
# -------------------------------------------------------------

def main():
    st.set_page_config("🎬 Video Sansür + Bip" , layout="wide")
    st.title("🎬 Gerçek Zamanlı Video Sansür (Bip Overlay)")
    st.markdown(
        "Video sesindeki küfürlü kelimelerin tam üstüne 1 kHz bip yerleştirir, orijinal ses bozulmaz."
    )

    uploaded = st.file_uploader("Video dosyası seçin", ["mp4", "mkv", "mov", "avi", "webm"])
    if not uploaded:
        st.info("👈 Soldan bir video yükleyin.")
        return

    # Geçici video kaydet
    with tempfile.NamedTemporaryFile(delete=False, suffix="." + uploaded.name.split(".")[-1]) as tmp_vid:
        tmp_vid.write(uploaded.read())
        video_path = tmp_vid.name

    st.success("✅ Video yüklendi.")
    st.video(video_path)

    # Başlat/Durdur butonları
    col_a, col_b = st.columns(2)
    with col_a:
        start = st.button("🚀 Analizi Başlat", type="primary")
    with col_b:
        stop = st.button("⏹ Durdur")

    # Session flag
    if start:
        st.session_state.analysis = True
    if stop:
        st.session_state.analysis = False

    # Çıktı konteynerleri
    transcript_area = st.empty()
    metric_area = st.empty()

    # Analiz başlatıldıysa
    if st.session_state.get("analysis"):
        global is_processing
        if not is_processing:
            is_processing = True
            threading.Thread(target=audio_processor_worker, daemon=True).start()
            st.info("🔊 Ses analizine başlandı…")

            chunk_gen = extract_audio_chunks_from_video(video_path)
            combined_paths = []
            total_bad = set()
            processed = 0

            for chunk in chunk_gen:
                if not st.session_state.get("analysis"):
                    break
                processing_queue.put(chunk)
                try:
                    res = results_queue.get(timeout=10)
                    processed += 1
                    combined_paths.append(res["censored_audio_path"])
                    total_bad.update(res["bad_words"])
                    # Anlık güncelleme
                    transcript_area.write(res["masked_text"])
                    metric_area.metric("İşlenen Parça", processed)
                except queue.Empty:
                    continue

            # Temizle
            is_processing = False
            processing_queue.put(None)

            # Birleştir ve göster
            final_audio = combine_audio_segments(combined_paths)
            st.subheader("🔊 Sansürlü Ses Çıktısı")
            with open(final_audio, "rb") as f:
                st.audio(f.read())
                st.download_button("⬇ Sesi İndir", f, "sansurlu.wav", "audio/wav")

    # Sidebar açıklama
    with st.sidebar:
        st.markdown("## Nasıl çalışır?\n1. Video yükle\n2. Analizi başlat – küfür anları tespit edilir, bip eklenir\n3. Çıkış sesini dinle/indir")

if __name__ == "__main__":
    main()
