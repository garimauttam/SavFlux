/**
 * VoiceControls.tsx — Speech-to-text chat input (P2 Voice, $0).
 *
 * Uses the browser's built-in SpeechRecognition (Chrome/Edge) — no API key,
 * no network dependency beyond the browser's own speech service. Final
 * transcripts are appended to the chat input via onTranscript. The button
 * hides itself where the API is unavailable (Firefox/Safari desktop).
 */

import { useEffect, useRef, useState } from "react";
import { Mic, MicOff } from "lucide-react";

interface VoiceControlsProps {
  onTranscript: (text: string) => void;
  disabled?: boolean;
}

// Minimal local typings — SpeechRecognition isn't in every TS DOM lib.
interface SpeechRecognitionLike {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((e: any) => void) | null;
  onerror: ((e: any) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
}

function recognitionCtor(): (new () => SpeechRecognitionLike) | null {
  try {
    const w = window as any;
    return (w.SpeechRecognition || w.webkitSpeechRecognition || null) as
      | (new () => SpeechRecognitionLike)
      | null;
  } catch {
    return null;
  }
}

export function VoiceControls({ onTranscript, disabled }: VoiceControlsProps) {
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const cbRef = useRef(onTranscript);
  cbRef.current = onTranscript;

  const supported = recognitionCtor() !== null;

  useEffect(() => {
    // Abort any in-flight recognition on unmount
    return () => {
      try {
        recRef.current?.abort();
      } catch {}
    };
  }, []);

  if (!supported) return null;

  const toggle = () => {
    if (listening) {
      try {
        recRef.current?.stop();
      } catch {}
      setListening(false);
      return;
    }
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    setError(null);
    try {
      const rec = new Ctor();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      rec.onresult = (e: any) => {
        try {
          const results = e?.results;
          const last = results?.[results.length - 1];
          const text = last?.[0]?.transcript?.trim();
          if (text) cbRef.current(text);
        } catch {}
      };
      rec.onerror = (e: any) => {
        const code = e?.error;
        if (code === "not-allowed" || code === "service-not-allowed") {
          setError("Microphone blocked — allow mic access to dictate.");
        } else if (code && code !== "aborted" && code !== "no-speech") {
          setError(`Dictation error: ${code}`);
        }
        setListening(false);
      };
      rec.onend = () => setListening(false);
      recRef.current = rec;
      rec.start();
      setListening(true);
    } catch {
      setError("Could not start dictation in this browser.");
      setListening(false);
    }
  };

  return (
    <span className="inline-flex shrink-0 items-center gap-1">
      <button
        onClick={toggle}
        disabled={disabled}
        title={listening ? "Stop dictation" : "Dictate with your microphone"}
        aria-pressed={listening}
        className={`flex h-11 w-11 items-center justify-center rounded-xl border transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
          listening
            ? "border-red-500/60 bg-red-500/15 text-red-300 animate-pulse"
            : "border-gray-600 bg-gray-800 text-gray-300 hover:border-purple-500 hover:text-white"
        }`}
      >
        {listening ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
      </button>
      {error && (
        <span className="max-w-[140px] text-[11px] leading-tight text-red-300" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

export default VoiceControls;
