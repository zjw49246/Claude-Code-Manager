import { useState, useRef } from 'react';
import { Mic, MicOff, Loader } from '../icons';

interface VoiceButtonProps {
  onTranscribed: (text: string) => void;
}

export function VoiceButton({ onTranscribed }: VoiceButtonProps) {
  const [recording, setRecording] = useState(false);
  const [loading, setLoading] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      mediaRecorderRef.current = mediaRecorder;
      chunksRef.current = [];

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: 'audio/webm' });
        if (blob.size === 0) return;

        setLoading(true);
        try {
          const formData = new FormData();
          formData.append('file', blob, 'audio.webm');
          const res = await fetch('/api/voice/transcribe', { method: 'POST', body: formData });
          if (res.ok) {
            const data = await res.json();
            if (data.text) onTranscribed(data.text);
          }
        } catch (err) {
          console.error('Transcription error:', err);
        } finally {
          setLoading(false);
        }
      };

      mediaRecorder.start();
      setRecording(true);
    } catch (err) {
      console.error('Microphone access denied:', err);
    }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
      mediaRecorderRef.current.stop();
    }
    setRecording(false);
  };

  const handleClick = () => {
    if (loading) return;
    if (recording) {
      stopRecording();
    } else {
      startRecording();
    }
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={loading}
      className={`p-2 rounded transition-colors ${
        recording
          ? 'bg-red-500 text-white animate-pulse'
          : loading
            ? 'bg-gray-600 text-gray-400'
            : 'bg-gray-700 text-gray-300 hover:bg-gray-600 hover:text-foreground'
      }`}
      title={recording ? 'Stop recording' : loading ? 'Transcribing...' : 'Voice input'}
    >
      {loading ? <Loader size={16} className="animate-spin" /> : recording ? <MicOff size={16} /> : <Mic size={16} />}
    </button>
  );
}
