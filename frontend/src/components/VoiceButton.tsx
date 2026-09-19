/**
 * VoiceButton.tsx — Per-message text-to-speech ($0, browser speechSynthesis).
 *
 * Reads an assistant answer aloud. No API key, no network, no deps:
 * window.speechSynthesis is built into every desktop browser. The button
 * hides itself when the API is unavailable (e.g. some embedded webviews).
 */

import { useCallback, useEffect, useState } from "react";
import { Volume2, Square } from "lucide-react";

interface VoiceButtonProps {
  text: string;
  isStreaming?: boolean;
}

function supported(): boolean {
  try {
    return typeof window !== "undefined" && "speechSynthesis" in window;
  } catch {
    return false;
  }
}

/** Strip markdown/code fences so the spoken output sounds natural. */
function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " code block omitted ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[#>*_`~|-]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 4000); // browsers cap utterance length; stay safely under it
}

export function VoiceButton({ text, isStreaming }: VoiceButtonProps) {
  const [speaking, setSpeaking] = useState(false);

  const stop = useCallback(() => {
    try {
      window.speechSynthesis.cancel();
    } catch {}
    setSpeaking(false);
  }, []);

  // Stop when the message re-streams or the component unmounts
  useEffect(() => {
    if (isStreaming) stop();
  }, [isStreaming, stop]);
  useEffect(() => stop, [stop]);

  if (!supported()) return null;

  const toggle = () => {
    if (speaking) {
      stop();
      return;
    }
    const clean = speakable(text);
    if (!clean) return;
    try {
      window.speechSynthesis.cancel(); // one utterance at a time app-wide
      const utterance = new SpeechSynthesisUtterance(clean);
      utterance.onend = () => setSpeaking(false);
      utterance.onerror = () => setSpeaking(false);
      window.speechSynthesis.speak(utterance);
      setSpeaking(true);
    } catch {
      setSpeaking(false);
    }
  };

  return (
    <button
      onClick={toggle}
      disabled={isStreaming}
      title={speaking ? "Stop reading aloud" : "Read answer aloud"}
      className={`flex items-center gap-1 rounded-full border px-2 py-1 text-[11px] transition-colors disabled:opacity-40 ${
        speaking
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
          : "border-gray-700 bg-gray-800 text-gray-400 hover:text-white"
      }`}
    >
      {speaking ? <Square className="w-3 h-3" /> : <Volume2 className="w-3 h-3" />}
      {speaking ? "Stop" : "Listen"}
    </button>
  );
}

export default VoiceButton;
