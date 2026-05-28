/**
 * Stream-style live voice visualizer (see getstream.io React AI voice assistant tutorial, step 6).
 * RMS volume from Web Audio analysers drives scale/brightness; activity flips between
 * user mic (listening) and assistant output (speaking).
 */

export type VoiceVisualizerActivity = "listening" | "speaking";

const LISTENING_COOLDOWN_MS = 1000;
/** Mic RMS above this ⇒ treat visitor as speaking (when assistant is not). */
const USER_SPEAK_ENTER = 0.048;
const USER_SPEAK_EXIT = 0.032;

export interface VoiceVisualizerOptions {
  getMicAnalyser: () => AnalyserNode | null;
  getRemoteAnalyser: () => AnalyserNode | null;
  getAssistantSpeaking: () => boolean;
  getIsLive: () => boolean;
  /** Root for queries — typically #app */
  getRoot: () => ParentNode | null;
}

export interface VoiceVisualizerHandle {
  stop(): void;
}

function rmsFromByteAnalyser(analyser: AnalyserNode, timeData: Uint8Array): number {
  analyser.getByteTimeDomainData(timeData);
  let sumSq = 0;
  for (let i = 0; i < timeData.length; i++) {
    const v = (timeData[i]! - 128) / 128;
    sumSq += v * v;
  }
  return Math.sqrt(sumSq / timeData.length);
}

/** Closer to Stream's Float32 time-domain RMS (tutorial step 6.1). */
function rmsFromFloatAnalyser(analyser: AnalyserNode, data: Float32Array): number {
  analyser.getFloatTimeDomainData(data);
  return Math.sqrt(data.reduce((acc, amp) => acc + (amp * amp) / data.length, 0));
}

function clampVolume(raw: number, gain: number): number {
  let v = Math.min(1, raw * gain);
  if (v < 0.04) v *= 0.2;
  return v;
}

/** Tracks ambient RMS so long calls do not drift the envelope upward and re-expose syllable flicker. */
function trackNoiseFloor(floor: number, sample: number): number {
  if (sample <= floor) {
    return floor + (sample - floor) * 0.1;
  }
  return floor + (sample - floor) * 0.0015;
}

function subtractFloor(sample: number, floor: number): number {
  return Math.max(0, sample - floor * 1.22);
}

/** Attack when rising, faster release when falling — avoids smooth hugging an elevated baseline. */
function smoothAsymmetric(prev: number, target: number, attack: number, release: number): number {
  const rate = target > prev ? attack : release;
  return prev * (1 - rate) + target * rate;
}

export function startVoiceAudioVisualizer(
  opts: VoiceVisualizerOptions,
): VoiceVisualizerHandle {
  let raf: number | null = null;
  let activity: VoiceVisualizerActivity = "speaking";
  let listeningCooldown: ReturnType<typeof setTimeout> | null = null;
  let micSmooth = 0;
  let remoteSmooth = 0;
  let ampSmooth = 0;
  let blobXSmooth = 0;
  let blobYSmooth = 0;
  let micFloor = 0;
  let remoteFloor = 0;
  let userSpeakingLatched = false;
  const sessionStartedAt = performance.now();

  const micByteBuf = new Uint8Array(512);
  let remoteFloatBuf: Float32Array | null = null;

  function clearListeningCooldown(): void {
    if (listeningCooldown !== null) {
      clearTimeout(listeningCooldown);
      listeningCooldown = null;
    }
  }

  function applyAuraActivity(root: ParentNode, next: VoiceVisualizerActivity): void {
    root.querySelectorAll<HTMLElement>(".voice-audio-visualizer__aura").forEach((aura) => {
      aura.classList.remove(
        "voice-audio-visualizer__aura_listening",
        "voice-audio-visualizer__aura_speaking",
      );
      aura.classList.add(
        next === "listening"
          ? "voice-audio-visualizer__aura_listening"
          : "voice-audio-visualizer__aura_speaking",
      );
    });
    root.querySelectorAll<HTMLElement>(".landing-cta__voice-orb").forEach((orb) => {
      orb.classList.remove("is-listening", "is-speaking");
      orb.classList.add(next === "listening" ? "is-listening" : "is-speaking");
    });
  }

  function setActivity(next: VoiceVisualizerActivity): void {
    if (activity === next) return;
    activity = next;
    const root = opts.getRoot();
    if (!root) return;
    const viz =
      root.querySelector<HTMLElement>(".voice-stage__visualizer") ??
      root.querySelector<HTMLElement>(".voice-audio-visualizer");
    const pillOrb = root.querySelector<HTMLElement>(".landing-cta__voice-orb");
    const stage =
      root.querySelector<HTMLElement>(".voice-stage--live") ??
      root.querySelector<HTMLElement>(".voice-stage");
    viz?.classList.toggle("is-activity-listening", next === "listening");
    viz?.classList.toggle("is-activity-speaking", next === "speaking");
    pillOrb?.classList.toggle("is-activity-listening", next === "listening");
    pillOrb?.classList.toggle("is-activity-speaking", next === "speaking");
    stage?.classList.toggle("is-user-listening", next === "listening");
    root.querySelector<HTMLElement>(".call-stack--live")?.classList.toggle(
      "is-user-listening",
      next === "listening",
    );
    applyAuraActivity(root, next);
  }

  function tick(): void {
    raf = requestAnimationFrame(tick);
    if (!opts.getIsLive()) return;

    const micAnalyser = opts.getMicAnalyser();
    const remoteAnalyser = opts.getRemoteAnalyser();

    let micInst = 0;
    if (micAnalyser) {
      micInst = clampVolume(rmsFromByteAnalyser(micAnalyser, micByteBuf), 6.2);
    }

    let remoteInst = 0;
    if (remoteAnalyser) {
      if (!remoteFloatBuf || remoteFloatBuf.length !== remoteAnalyser.fftSize) {
        remoteFloatBuf = new Float32Array(remoteAnalyser.fftSize);
      }
      remoteInst = clampVolume(
        rmsFromFloatAnalyser(remoteAnalyser, remoteFloatBuf),
        7,
      );
    }

    micFloor = trackNoiseFloor(micFloor, micInst);
    remoteFloor = trackNoiseFloor(remoteFloor, remoteInst);
    const micSignal = subtractFloor(micInst, micFloor);
    const remoteSignal = subtractFloor(remoteInst, remoteFloor);

    /* Asymmetric envelopes: slow attack, faster release — stops long-call baseline creep. */
    if (activity === "listening") {
      micSmooth = smoothAsymmetric(micSmooth, micSignal, 0.05, 0.11);
    } else {
      micSmooth = smoothAsymmetric(micSmooth, micSignal, 0.14, 0.22);
    }
    if (activity === "speaking") {
      remoteSmooth = smoothAsymmetric(remoteSmooth, remoteSignal, 0.045, 0.1);
    } else {
      remoteSmooth = smoothAsymmetric(remoteSmooth, remoteSignal, 0.1, 0.16);
    }

    const assistant = opts.getAssistantSpeaking();
    if (assistant) {
      clearListeningCooldown();
      setActivity("speaking");
    } else if (micSmooth > USER_SPEAK_ENTER) {
      userSpeakingLatched = true;
      clearListeningCooldown();
      setActivity("listening");
    } else {
      if (micSmooth < USER_SPEAK_EXIT) {
        userSpeakingLatched = false;
      }
      if (userSpeakingLatched && !assistant) {
        clearListeningCooldown();
        setActivity("listening");
      } else if (activity === "listening") {
        if (listeningCooldown === null) {
          listeningCooldown = setTimeout(() => {
            listeningCooldown = null;
            setActivity("speaking");
          }, LISTENING_COOLDOWN_MS);
        }
      }
    }
    let displayVol =
      activity === "listening" ? micSmooth : Math.max(remoteSmooth, assistant ? remoteSmooth : 0);

    /* Idle “breathing” when assistant is quiet (Stream-style ambient pulse). */
    if (activity === "speaking" && !assistant && remoteSmooth < 0.06) {
      const breath = 0.07 + Math.sin(performance.now() / 1800) * 0.028;
      displayVol = Math.max(displayVol, breath);
    }

    const speaking = activity === "speaking";
    const listening = activity === "listening";
    const reactive = speaking || listening;
    /* Heavier smoothing as the call runs — WebRTC noise floor often rises over time. */
    const callMinutes = (performance.now() - sessionStartedAt) / 60_000;
    const ampBlend = reactive
      ? Math.min(0.028, 0.018 + callMinutes * 0.0012)
      : 0.18;
    const prevAmp = ampSmooth;
    ampSmooth = prevAmp * (1 - ampBlend) + displayVol * ampBlend;
    /* Cap per-frame brightness jumps (syllable strobing) without killing call-start responsiveness. */
    if (reactive) {
      const ampCap = 0.006 + callMinutes * 0.00035;
      const step = ampSmooth - prevAmp;
      if (Math.abs(step) > ampCap) {
        ampSmooth = prevAmp + Math.sign(step) * ampCap;
      }
    }
    const ampUi = reactive ? ampSmooth : displayVol;
    /* Compress peaks before CSS — keeps glow alive without syllable strobing. */
    const ampCss = reactive ? Math.min(1, Math.sqrt(ampUi) * 0.88 + 0.06) : ampUi;
    const blobVol = reactive ? ampSmooth : displayVol;

    const scale = reactive
      ? Math.min(1 + ampCss * 0.08, 1.015)
      : Math.min(1 + ampUi * 0.55, 1.08);
    const brightness = reactive
      ? Math.max(Math.min(1 + ampCss * 0.035, 1.015), 1)
      : Math.max(Math.min(1 + ampUi * 0.38, 1.1), 1);
    const amp = ampCss.toFixed(3);
    const scaleStr = scale.toFixed(4);
    const brightStr = brightness.toFixed(4);

    const root = opts.getRoot();
    if (!root) return;

    const viz =
      root.querySelector<HTMLElement>(".voice-stage__visualizer") ??
      root.querySelector<HTMLElement>(".voice-audio-visualizer");
    const pillOrb = root.querySelector<HTMLElement>(".landing-cta__voice-orb");
    const pillChamber = root.querySelector<HTMLElement>(
      "#footerPrimary.voice-live-end.is-live .landing-cta__wave-slot",
    );
    const host = root.querySelector<HTMLElement>(".call-stack--live");
    const stage =
      root.querySelector<HTMLElement>(".voice-stage--live") ??
      root.querySelector<HTMLElement>(".voice-stage");
    const heroGlow = root.querySelector<HTMLElement>(".call-hero-zone");
    const liquidOrb = root.querySelector<HTMLElement>(".voice-liquid-orb");
    const footerEnd = root.querySelector<HTMLElement>("#footerPrimary.voice-live-end.is-live");
    const micReactiveCtas = root.querySelectorAll<HTMLElement>(".landing-cta.mic-reactive");

    const motionAmp = reactive ? ampSmooth : displayVol;
    const blobTargetX =
      speaking || listening ? motionAmp * 1.4 : micSmooth * 5.5;
    const blobTargetY = reactive ? motionAmp * 1.1 : micSignal * 4.5;
    const blobSmooth = reactive ? 0.97 : 0.84;
    const blobGain = reactive ? 0.03 : 0.16;
    blobXSmooth = blobXSmooth * blobSmooth + blobTargetX * blobGain;
    blobYSmooth = blobYSmooth * blobSmooth + blobTargetY * blobGain;
    const blobX = blobXSmooth.toFixed(3);
    const blobY = blobYSmooth.toFixed(3);
    const t = performance.now() / 1000;
    const blobR = (
      blobVol * (reactive ? 4 : 12) +
      Math.sin(t * 2.4) * (reactive ? 2 : 3.5) +
      Math.cos(t * 1.55) * (reactive ? 1.5 : 2.5)
    ).toFixed(2);
    const morphSpeed = Math.max(
      reactive ? 2.6 : 2.1,
      (reactive ? 5.4 : 4.6) - blobVol * (reactive ? 1.2 : 3.4),
    ).toFixed(2);
    const warpX = (
      blobVol * (reactive ? 0.02 : 0.07) +
      Math.sin(t * 3.1) * (reactive ? 0.03 : 0.055)
    ).toFixed(3);
    const warpY = (
      blobVol * (reactive ? 0.018 : 0.06) +
      Math.cos(t * 2.35) * (reactive ? 0.028 : 0.05)
    ).toFixed(3);
    const skew = (
      blobVol * (reactive ? 1.2 : 4.5) +
      Math.sin(t * 2.85) * (reactive ? 1.5 : 2.8)
    ).toFixed(2);

    const blobTargets = [viz, stage, heroGlow, liquidOrb, pillOrb, pillChamber, footerEnd].filter(
      Boolean,
    ) as HTMLElement[];
    for (const el of blobTargets) {
      el.style.setProperty("--voice-blob-x", blobX);
      el.style.setProperty("--voice-blob-y", blobY);
      el.style.setProperty("--voice-blob-r", blobR);
      el.style.setProperty("--voice-blob-morph-speed", `${morphSpeed}s`);
      el.style.setProperty("--voice-blob-morph-speed-b", `${(parseFloat(morphSpeed) * 1.35).toFixed(2)}s`);
      el.style.setProperty("--voice-blob-warp-x", warpX);
      el.style.setProperty("--voice-blob-warp-y", warpY);
      el.style.setProperty("--voice-blob-skew", skew);
    }

    if (viz) {
      viz.style.setProperty("--volumeter-scale", scaleStr);
      viz.style.setProperty("--volumeter-brightness", brightStr);
      viz.style.setProperty("--voice-amp", amp);
    }
    if (pillOrb) {
      pillOrb.style.setProperty("--volumeter-scale", scaleStr);
      pillOrb.style.setProperty("--volumeter-brightness", brightStr);
      pillOrb.style.setProperty("--voice-amp", amp);
      pillOrb.classList.toggle("is-activity-listening", activity === "listening");
      pillOrb.classList.toggle("is-activity-speaking", activity === "speaking");
    }
    if (footerEnd) {
      footerEnd.style.setProperty("--voice-amp", amp);
      footerEnd.classList.toggle("is-assistant-speaking", assistant);
      footerEnd.classList.toggle("is-user-listening", activity === "listening");
    }
    if (host) {
      host.style.setProperty("--voice-amp", amp);
      host.classList.toggle("is-assistant-speaking", assistant);
      host.classList.toggle("is-user-listening", activity === "listening");
    }
    if (stage) {
      stage.style.setProperty("--voice-amp", amp);
      stage.classList.toggle("is-assistant-speaking", assistant);
      stage.classList.toggle("is-user-listening", activity === "listening");
    }
    if (viz) {
      viz.classList.toggle("is-activity-listening", activity === "listening");
      viz.classList.toggle("is-activity-speaking", activity === "speaking");
    }
    if (heroGlow) {
      heroGlow.style.setProperty("--voice-amp", amp);
      heroGlow.classList.toggle("is-user-listening", activity === "listening");
      heroGlow.classList.toggle("is-assistant-speaking", assistant);
    }
    const micLevel = listening ? amp : micSmooth.toFixed(3);
    micReactiveCtas.forEach((cta) => {
      cta.style.setProperty("--mic-level", micLevel);
    });

    const ambient = root.querySelector<HTMLElement>(".voice-stage__ambient-listen");
    if (ambient) {
      ambient.style.opacity = (listening ? 0.58 + ampCss * 0.28 : 0).toFixed(3);
    }
  }

  setActivity("speaking");
  raf = requestAnimationFrame(tick);

  return {
    stop() {
      if (raf !== null) {
        cancelAnimationFrame(raf);
        raf = null;
      }
      clearListeningCooldown();
      micSmooth = 0;
      remoteSmooth = 0;
      ampSmooth = 0;
      blobXSmooth = 0;
      blobYSmooth = 0;
      micFloor = 0;
      remoteFloor = 0;
      userSpeakingLatched = false;
      const root = opts.getRoot();
      if (!root) return;
      for (const el of [
        root.querySelector<HTMLElement>(".call-stack--live"),
        root.querySelector<HTMLElement>(".voice-stage--live"),
        root.querySelector<HTMLElement>(".voice-audio-visualizer"),
        root.querySelector<HTMLElement>(".landing-cta__voice-orb"),
        root.querySelector<HTMLElement>(".call-hero-zone"),
        root.querySelector<HTMLElement>("#footerPrimary"),
        root.querySelector<HTMLElement>("#footerPrimary.voice-live-end.is-live"),
      ]) {
        if (!el) continue;
        el.style.removeProperty("--volumeter-scale");
        el.style.removeProperty("--volumeter-brightness");
        el.style.removeProperty("--voice-amp");
        el.style.removeProperty("--voice-blob-x");
        el.style.removeProperty("--voice-blob-y");
        el.style.removeProperty("--voice-blob-r");
        el.style.removeProperty("--voice-blob-morph-speed");
        el.style.removeProperty("--voice-blob-morph-speed-b");
        el.style.removeProperty("--voice-blob-warp-x");
        el.style.removeProperty("--voice-blob-warp-y");
        el.style.removeProperty("--voice-blob-skew");
        el.classList.remove(
          "is-user-listening",
          "is-assistant-speaking",
          "is-activity-listening",
          "is-activity-speaking",
        );
      }
      root.querySelector(".voice-stage")?.classList.remove("is-user-listening");
      root.querySelector(".voice-audio-visualizer")?.classList.remove(
        "is-activity-listening",
        "is-activity-speaking",
      );
    },
  };
}
