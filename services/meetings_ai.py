"""
Gemini AI Multimodal Transcription & Formal English Minutes Service.
Handles:
- Audio file upload to Gemini API
- Structured JSON meeting minutes generation
- MANDATORY cleanup of both local temp files and Gemini cloud files
"""

import os
import json
import shutil
import logging
import asyncio
from typing import Optional, Dict, Any, List

from config import Config

logger = logging.getLogger("MeetingsAI")


def _build_gemini_client():
    """Creates a Gemini API client. Must be called inside asyncio.to_thread()."""
    from google import genai
    return genai.Client(api_key=Config.GEMINI_API_KEY)


MEETING_MINUTES_PROMPT = """You are a professional meeting transcription and minutes generation assistant.

CONTEXT:
- This is an audio recording from a team meeting or training session.
- Speakers communicate in Bangla (Bengali), English, and mixed Banglish.
- Common discussion topics include 2D/3D data annotation, quality control, project codes, and team operations.

YOUR TASK:
Analyze the entire audio recording and produce a comprehensive, structured meeting minutes document.
ALL output MUST be in strictly professional, formal English — regardless of the input language.

OUTPUT SCHEMA (strict JSON):
{
  "executive_summary": "A concise formal English paragraph summarizing the entire meeting — purpose, key discussions, outcomes, and overall direction.",
  "key_points": ["Array of key discussion points, decisions, and important topics covered — each as a formal English string."],
  "action_items": ["Array of action items with assigned owner and deadline where mentioned — each as a formal English string, e.g. 'John to complete QC review of Project X by Friday.'"],
  "qa_and_decisions": ["Array of questions raised, answers given, and decisions made — each as a formal English string."],
  "full_transcript": "Complete chronological speaker-labeled transcript in clear formal English. Format each speaker turn as 'Speaker Name: What they said.' on a new line. Translate all Bangla/Banglish speech into formal English."
}

IMPORTANT RULES:
1. Translate ALL Bangla/Banglish content into formal English.
2. Identify and label speakers by voice where possible (Speaker 1, Speaker 2, etc.).
3. Include timestamps or sequence markers in the transcript where discernible.
4. If the audio quality is poor or inaudible in sections, note it as [inaudible] or [unclear].
5. Be thorough — do not skip or summarize portions of the discussion in the transcript.
6. Return ONLY the JSON object, no markdown wrapping or additional text."""


def _parse_json_response(raw_response: str) -> Dict[str, Any]:
    """Parses Gemini response text into structured dictionary, handling markdown wrapping and fallbacks."""
    if not raw_response or not raw_response.strip():
        return {
            "executive_summary": "No transcript available (empty response).",
            "key_points": [],
            "action_items": [],
            "qa_and_decisions": [],
            "full_transcript": ""
        }

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError:
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw_response)
        if json_match:
            try:
                result = json.loads(json_match.group())
            except Exception:
                result = {
                    "executive_summary": raw_response[:1000],
                    "key_points": [],
                    "action_items": [],
                    "qa_and_decisions": [],
                    "full_transcript": raw_response
                }
        else:
            result = {
                "executive_summary": raw_response[:1000],
                "key_points": [],
                "action_items": [],
                "qa_and_decisions": [],
                "full_transcript": raw_response
            }

    required_keys = ["executive_summary", "key_points", "action_items", "qa_and_decisions", "full_transcript"]
    for key in required_keys:
        if key not in result:
            result[key] = "" if key in ("executive_summary", "full_transcript") else []

    for key in ["key_points", "action_items", "qa_and_decisions"]:
        if not isinstance(result[key], list):
            result[key] = [str(result[key])] if result[key] else []

    return result


async def generate_meeting_minutes(
    audio_path: str,
    meeting_temp_dir: str,
    speakers_hint: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Uploads audio to Gemini, generates structured meeting minutes, and
    MANDATORILY cleans up both local temp files and Gemini cloud files.

    Returns dict with keys: executive_summary, key_points, action_items,
    qa_and_decisions, full_transcript, or raises Exception.
    """
    if not Config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured. Set it in your .env file.")

    uploaded_file = None
    client = None

    try:
        # All Gemini operations run in a background thread to avoid blocking the event loop
        def _upload_and_generate():
            nonlocal uploaded_file, client
            client = _build_gemini_client()

            # Upload the compressed audio file
            logger.info(f"[AI] Uploading audio file: {audio_path}")
            uploaded_file = client.files.upload(file=audio_path)
            logger.info(f"[AI] Upload complete: {uploaded_file.name}")

            # Build prompt with optional speaker hints
            prompt = MEETING_MINUTES_PROMPT
            if speakers_hint:
                hint_str = ", ".join(speakers_hint)
                prompt += f"\n\nKnown speakers in this meeting: {hint_str}. Use these names in the transcript where possible."

            # Build candidate models list: primary configured model first, followed by modern 2.x fallbacks
            configured_model = Config.GEMINI_MODEL or "gemini-2.0-flash"
            candidate_models = [configured_model]
            for fallback in ["gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash-lite", "gemini-2.5-flash", "gemini-1.5-flash"]:
                if fallback not in candidate_models:
                    candidate_models.append(fallback)

            last_err = None
            for model_name in candidate_models:
                try:
                    logger.info(f"[AI] Generating minutes with model: {model_name}")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[uploaded_file, prompt],
                        config={
                            "response_mime_type": "application/json",
                            "temperature": 0.2,
                        }
                    )
                    if response and response.text:
                        logger.info(f"[AI] Successfully generated minutes using model: {model_name}")
                        return response.text
                except Exception as model_err:
                    last_err = model_err
                    logger.warning(
                        f"[AI] Model '{model_name}' failed or overloaded ({model_err}). "
                        f"Attempting fallback model if available..."
                    )
                    continue

            if last_err:
                raise last_err
            raise RuntimeError("All Gemini candidate models failed to generate content.")

        raw_response = await asyncio.to_thread(_upload_and_generate)
        result = _parse_json_response(raw_response)
        logger.info(f"[AI] Minutes generated successfully. Summary length: {len(result.get('executive_summary', ''))}")
        return result

    finally:
        # ====================================================================
        # MANDATORY CLEANUP — This block ALWAYS runs regardless of success/failure
        # ====================================================================

        # 1. Delete the uploaded file from Gemini cloud storage
        if uploaded_file and client:
            try:
                def _delete_remote():
                    client.files.delete(name=uploaded_file.name)
                await asyncio.to_thread(_delete_remote)
                logger.info(f"[AI] Cleaned up Gemini cloud file: {uploaded_file.name}")
            except Exception as cleanup_err:
                logger.warning(f"[AI] Failed to delete Gemini cloud file: {cleanup_err}")

        # 2. Delete all local temporary audio files and folders
        if meeting_temp_dir and os.path.exists(meeting_temp_dir):
            try:
                shutil.rmtree(meeting_temp_dir, ignore_errors=True)
                logger.info(f"[AI] Cleaned up local temp dir: {meeting_temp_dir}")
            except Exception as cleanup_err:
                logger.warning(f"[AI] Failed to clean local temp dir: {cleanup_err}")


async def compress_audio_ffmpeg(input_path: str, output_path: str) -> str:
    """
    Compresses audio to lightweight mono format using ffmpeg subprocess.
    If ffmpeg is not installed on system, falls back to using the uncompressed audio file directly.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", input_path,
            "-ac", "1",        # mono
            "-ar", "16000",    # 16kHz sample rate
            "-b:a", "32k",     # 32kbps bitrate
            "-vn",             # no video
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace")[-500:]
            logger.warning(f"[AI] ffmpeg compression failed (exit {proc.returncode}): {error_msg}. Using uncompressed audio.")
            shutil.copy2(input_path, output_path)
            return output_path

        logger.info(f"[AI] Audio compressed: {input_path} -> {output_path}")
        return output_path

    except FileNotFoundError:
        logger.warning("[AI] ffmpeg is not installed in system PATH. Proceeding with uncompressed audio directly for Gemini AI.")
        shutil.copy2(input_path, output_path)
        return output_path


async def merge_audio_files_ffmpeg(input_files: List[str], output_path: str) -> str:
    """
    Merges multiple audio files into one. Uses ffmpeg concat if available,
    or Python built-in wave concat fallback if ffmpeg is missing.
    """
    if not input_files:
        raise ValueError("No audio files to merge.")

    if len(input_files) == 1:
        return await compress_audio_ffmpeg(input_files[0], output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Create a concat list file for ffmpeg
    list_path = output_path + ".list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for fp in input_files:
            safe_path = fp.replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-ac", "1", "-ar", "16000", "-b:a", "32k", "-vn",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode(errors="replace")[-500:]
            logger.warning(f"[AI] ffmpeg merge failed: {error_msg}. Falling back to Python audio merge.")
            return await _merge_wav_files_fallback(input_files, output_path)

        logger.info(f"[AI] Merged {len(input_files)} audio files -> {output_path}")
        return output_path

    except FileNotFoundError:
        logger.warning("[AI] ffmpeg not installed. Merging audio using Python wave fallback.")
        return await _merge_wav_files_fallback(input_files, output_path)

    finally:
        # Clean up list file
        try:
            if os.path.exists(list_path):
                os.remove(list_path)
        except OSError:
            pass


async def _merge_wav_files_fallback(input_files: List[str], output_path: str) -> str:
    """Fallback to merge multiple WAV files using Python built-in wave module without ffmpeg."""
    def _do_merge():
        import wave
        wav_files = [f for f in input_files if f.lower().endswith(".wav") and os.path.exists(f)]
        if not wav_files:
            # If not wav, copy the first available file
            shutil.copy2(input_files[0], output_path)
            return output_path

        first = wav_files[0]
        with wave.open(first, "rb") as w0:
            params = w0.getparams()

        # Ensure output is wav extension for wave module
        actual_output = output_path
        if not actual_output.lower().endswith(".wav"):
            actual_output = os.path.splitext(output_path)[0] + ".wav"

        with wave.open(actual_output, "wb") as wout:
            wout.setparams(params)
            for wf_path in wav_files:
                try:
                    with wave.open(wf_path, "rb") as win:
                        wout.writeframes(win.readframes(win.getnframes()))
                except Exception as err:
                    logger.warning(f"[AI] Could not append {wf_path}: {err}")

        return actual_output

    return await asyncio.to_thread(_do_merge)
