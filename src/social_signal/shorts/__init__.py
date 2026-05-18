"""YouTube Shorts pipeline for Glitch — text → script → visuals → voice → mp4.

Architecture:
  1. script_writer.py — LLM produces {hook, segments[], cta} JSON. Pulls
     voice + identity prompts the same way the comment drafters do, so a
     short reads like Tejas (or the lab) wrote it.
  2. visuals.py — gpt-image-2 produces one 1080x1920 still per segment.
     Brand chrome consistent across the deck.
  3. voice.py — ElevenLabs TTS produces one mp3 (single-take, faster
     than per-segment + cleaner audio).
  4. assembler.py — ffmpeg composites stills (Ken Burns zoom/pan) +
     voiceover + burned-in captions → 1080x1920 mp4 ready for Shorts.
  5. pipeline.py — orchestrator. CLI today; Discord HITL + sheet
     trigger added next.

Output: /var/lib/ai-social-media-agent/videos/shorts/<brand>/<uuid>.mp4
"""
