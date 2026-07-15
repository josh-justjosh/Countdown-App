/** Encode Float32 PCM to WAV Blob */
export function encodeWav(samples, sampleRate = 48000) {
  const numChannels = 1;
  const bytesPerSample = 2;
  const blockAlign = numChannels * bytesPerSample;
  const buffer = new ArrayBuffer(44 + samples.length * bytesPerSample);
  const view = new DataView(buffer);

  function writeString(offset, str) {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
  }

  writeString(0, "RIFF");
  view.setUint32(4, 36 + samples.length * bytesPerSample, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, samples.length * bytesPerSample, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

export async function decodeToMono(source) {
  const arrayBuf = source instanceof ArrayBuffer
    ? source.slice(0)
    : await source.arrayBuffer();
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const decoded = await audioCtx.decodeAudioData(arrayBuf.slice(0));
    const channel = decoded.getChannelData(0);
    return {
      samples: new Float32Array(channel),
      sampleRate: decoded.sampleRate,
      duration: decoded.duration,
    };
  } finally {
    await audioCtx.close();
  }
}

export function trimWav(samples, sampleRate, startSec, endSec) {
  const start = Math.max(0, Math.floor(startSec * sampleRate));
  const end = Math.min(samples.length, Math.ceil(endSec * sampleRate));
  if (end <= start + 1) {
    throw new Error("Selection too short");
  }
  return encodeWav(samples.subarray(start, end), sampleRate);
}

export function buildPeaks(samples, bucketCount = 400) {
  const n = Math.max(1, Math.min(bucketCount, samples.length));
  const peaks = new Float32Array(n);
  const block = samples.length / n;
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * block);
    const b = Math.floor((i + 1) * block);
    let peak = 0;
    for (let j = a; j < b; j++) {
      const v = Math.abs(samples[j]);
      if (v > peak) peak = v;
    }
    peaks[i] = peak;
  }
  return peaks;
}

/** Suggest in/out from simple RMS envelope (speech-ish region). */
export function suggestTrimBounds(samples, sampleRate, {
  frameMs = 20,
  silenceRatio = 0.08,
  padMs = 40,
} = {}) {
  const frame = Math.max(1, Math.floor((frameMs / 1000) * sampleRate));
  const energies = [];
  for (let i = 0; i < samples.length; i += frame) {
    let sum = 0;
    const end = Math.min(samples.length, i + frame);
    for (let j = i; j < end; j++) sum += samples[j] * samples[j];
    energies.push(Math.sqrt(sum / Math.max(1, end - i)));
  }
  const peak = Math.max(...energies, 1e-9);
  const thresh = peak * silenceRatio;
  let first = energies.findIndex((e) => e >= thresh);
  let last = energies.length - 1;
  for (let i = energies.length - 1; i >= 0; i--) {
    if (energies[i] >= thresh) {
      last = i;
      break;
    }
  }
  if (first < 0) {
    return { startSec: 0, endSec: samples.length / sampleRate };
  }
  const pad = padMs / 1000;
  const startSec = Math.max(0, (first * frame) / sampleRate - pad);
  const endSec = Math.min(
    samples.length / sampleRate,
    ((last + 1) * frame) / sampleRate + pad,
  );
  if (endSec - startSec < 0.05) {
    return { startSec: 0, endSec: samples.length / sampleRate };
  }
  return { startSec, endSec };
}

/** Peak-normalise an entire clip to a consistent level. */
export function normalizePeak(samples, {
  targetPeak = 0.82,
  maxGain = 10,
} = {}) {
  if (!samples?.length) return samples;
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    const v = Math.abs(samples[i]);
    if (v > peak) peak = v;
  }
  if (peak < 1e-6) return samples;
  const g = Math.min(maxGain, targetPeak / peak);
  if (Math.abs(g - 1) < 0.02) return samples;
  const out = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    out[i] = Math.max(-1, Math.min(1, samples[i] * g));
  }
  return out;
}

/**
 * Peak-normalise each spoken burst so quieter digits match louder ones.
 * Designed for continuous 10→1 takes with short gaps between numbers.
 */
export function normalizeSegmentPeaks(samples, sampleRate, {
  targetPeak = 0.82,
  frameMs = 20,
  silenceRatio = 0.10,
  minGapMs = 60,
  minSegMs = 60,
  maxGain = 10,
} = {}) {
  if (!samples?.length) return samples;
  const frame = Math.max(1, Math.floor((frameMs / 1000) * sampleRate));
  const energies = [];
  for (let i = 0; i < samples.length; i += frame) {
    let sum = 0;
    const end = Math.min(samples.length, i + frame);
    for (let j = i; j < end; j++) sum += samples[j] * samples[j];
    energies.push(Math.sqrt(sum / Math.max(1, end - i)));
  }
  const energyPeak = Math.max(...energies, 1e-9);
  const thresh = energyPeak * silenceRatio;
  const voiced = energies.map((e) => e >= thresh);

  const minGapFrames = Math.max(1, Math.round(minGapMs / frameMs));
  const minSegFrames = Math.max(1, Math.round(minSegMs / frameMs));
  const segments = [];
  let i = 0;
  while (i < voiced.length) {
    while (i < voiced.length && !voiced[i]) i += 1;
    if (i >= voiced.length) break;
    let start = i;
    let end = i;
    while (end < voiced.length) {
      if (voiced[end]) {
        end += 1;
        continue;
      }
      let gap = 0;
      while (end + gap < voiced.length && !voiced[end + gap]) gap += 1;
      if (gap >= minGapFrames) break;
      end += gap;
    }
    if (end - start >= minSegFrames) {
      segments.push({
        start: start * frame,
        end: Math.min(samples.length, end * frame),
      });
    }
    i = end;
  }

  const out = new Float32Array(samples);
  if (!segments.length) {
    // Fallback: whole-clip peak normalize
    let peak = 0;
    for (let j = 0; j < out.length; j++) {
      const v = Math.abs(out[j]);
      if (v > peak) peak = v;
    }
    if (peak > 1e-6) {
      const g = Math.min(maxGain, targetPeak / peak);
      for (let j = 0; j < out.length; j++) out[j] *= g;
    }
    return out;
  }

  for (const seg of segments) {
    let peak = 0;
    for (let j = seg.start; j < seg.end; j++) {
      const v = Math.abs(out[j]);
      if (v > peak) peak = v;
    }
    if (peak < 1e-6) continue;
    const g = Math.min(maxGain, targetPeak / peak);
    if (Math.abs(g - 1) < 0.02) continue;
    for (let j = seg.start; j < seg.end; j++) {
      out[j] = Math.max(-1, Math.min(1, out[j] * g));
    }
  }
  return out;
}

export async function blobToWav(blob, targetRate = 48000) {
  const { samples, sampleRate } = await decodeToMono(blob);
  let out = samples;
  if (sampleRate !== targetRate) {
    const ratio = targetRate / sampleRate;
    const newLen = Math.floor(samples.length * ratio);
    out = new Float32Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const src = i / ratio;
      const i0 = Math.floor(src);
      const i1 = Math.min(i0 + 1, samples.length - 1);
      const t = src - i0;
      out[i] = samples[i0] * (1 - t) + samples[i1] * t;
    }
  }
  return encodeWav(out, targetRate);
}

export class MicRecorder {
  constructor() {
    this.mediaRecorder = null;
    this.chunks = [];
    this.stream = null;
    this.analyser = null;
    this.audioCtx = null;
    this._raf = null;
    this.onLevel = null;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.chunks = [];
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : "audio/webm";
    this.mediaRecorder = new MediaRecorder(this.stream, { mimeType: mime });
    this.mediaRecorder.ondataavailable = (e) => {
      if (e.data.size) this.chunks.push(e.data);
    };
    this.mediaRecorder.start(100);

    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = this.audioCtx.createMediaStreamSource(this.stream);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 256;
    source.connect(this.analyser);
    this._meter();
  }

  _meter() {
    if (!this.analyser) return;
    const data = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      this.analyser.getByteFrequencyData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i];
      const level = sum / data.length / 255;
      if (this.onLevel) this.onLevel(level);
      this._raf = requestAnimationFrame(tick);
    };
    tick();
  }

  async stop() {
    if (this._raf) cancelAnimationFrame(this._raf);
    const recorder = this.mediaRecorder;
    if (!recorder) return null;
    const blob = await new Promise((resolve) => {
      recorder.onstop = () => {
        resolve(new Blob(this.chunks, { type: recorder.mimeType }));
      };
      recorder.stop();
    });
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.audioCtx?.close();
    this.mediaRecorder = null;
    this.stream = null;
    return blob;
  }
}
