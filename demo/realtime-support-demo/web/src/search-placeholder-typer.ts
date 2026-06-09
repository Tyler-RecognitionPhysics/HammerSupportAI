const DEFAULT_QUESTIONS = [
  "How do I reset my password?",
  "Where can I find my invoices?",
  "How do I add a new user?",
  "How do I update my billing info?",
  "What does Hammer CRM do?",
];

export interface SearchPlaceholderTyperOptions {
  idlePlaceholder: string;
  questions?: string[];
}

export interface SearchPlaceholderTyperHandle {
  stop(): void;
}

export function startSearchPlaceholderTyper(
  input: HTMLInputElement,
  options: SearchPlaceholderTyperOptions,
): SearchPlaceholderTyperHandle {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const shell = input.closest(".help-search");
  const questions = (options.questions ?? DEFAULT_QUESTIONS).filter((q) => q.trim());
  const idlePlaceholder = options.idlePlaceholder.trim() || "Ask a question about Hammer…";

  if (reducedMotion || !questions.length) {
    input.placeholder = idlePlaceholder;
    return { stop() {} };
  }

  let questionIndex = 0;
  let charIndex = 0;
  let deleting = false;
  let paused = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let cursorOn = true;
  let cursorTimer: ReturnType<typeof setInterval> | null = null;

  shell?.classList.add("is-placeholder-typing");

  const clearTimers = (): void => {
    if (timer) clearTimeout(timer);
    if (cursorTimer) clearInterval(cursorTimer);
    timer = null;
    cursorTimer = null;
  };

  const currentQuestion = (): string => questions[questionIndex % questions.length] ?? "";

  const renderPlaceholder = (): void => {
    if (paused || input.value || document.activeElement === input) return;
    const text = currentQuestion().slice(0, charIndex);
    input.placeholder = cursorOn && text ? `${text}|` : text || idlePlaceholder;
  };

  const schedule = (delay: number, fn: () => void): void => {
    timer = setTimeout(fn, delay);
  };

  const tick = (): void => {
    if (paused || input.disabled || input.value || document.activeElement === input) return;

    const question = currentQuestion();
    if (!question) return;

    if (!deleting && charIndex < question.length) {
      charIndex += 1;
      renderPlaceholder();
      schedule(52, tick);
      return;
    }

    if (!deleting && charIndex >= question.length) {
      schedule(2200, () => {
        deleting = true;
        tick();
      });
      return;
    }

    if (deleting && charIndex > 0) {
      charIndex -= 1;
      renderPlaceholder();
      schedule(28, tick);
      return;
    }

    deleting = false;
    questionIndex = (questionIndex + 1) % questions.length;
    schedule(420, tick);
  };

  const pause = (): void => {
    paused = true;
    input.placeholder = idlePlaceholder;
  };

  const resume = (): void => {
    if (input.value || input.disabled) return;
    paused = false;
    renderPlaceholder();
    schedule(320, tick);
  };

  const onInput = (): void => {
    if (input.value) pause();
    else if (document.activeElement !== input) resume();
  };

  input.addEventListener("focus", pause);
  input.addEventListener("blur", resume);
  input.addEventListener("input", onInput);

  cursorTimer = setInterval(() => {
    if (paused || input.value || document.activeElement === input) return;
    cursorOn = !cursorOn;
    renderPlaceholder();
  }, 530);

  renderPlaceholder();
  schedule(680, tick);

  return {
    stop() {
      clearTimers();
      shell?.classList.remove("is-placeholder-typing");
      input.removeEventListener("focus", pause);
      input.removeEventListener("blur", resume);
      input.removeEventListener("input", onInput);
      input.placeholder = idlePlaceholder;
    },
  };
}
