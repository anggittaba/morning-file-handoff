// Countdown to the deadline, busy state on long actions, copy button, and a quiet auto-refresh.
(function () {
  const clock = document.querySelector(".clock");
  if (clock && clock.dataset.frozen !== "1") {
    const deadline = new Date(clock.dataset.deadline).getTime();
    const serverNow = new Date(clock.dataset.now).getTime();
    const speed = parseFloat(clock.dataset.speed || "1");
    const started = Date.now();
    const value = clock.querySelector(".clock__value");
    const tick = () => {
      const now = serverNow + (Date.now() - started) * speed;
      const left = Math.round((deadline - now) / 1000);
      const s = Math.abs(left);
      value.textContent = (left < 0 ? "+" : "") + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    };
    tick();
    setInterval(tick, 1000);
  }

  document.querySelectorAll("form[data-busy]").forEach((form) => {
    form.addEventListener("submit", () => {
      const btn = form.querySelector("button[type=submit]");
      if (!btn) return;
      btn.setAttribute("aria-busy", "true");
      btn.disabled = true;
      setTimeout(() => { btn.textContent = form.dataset.busy; }, 400);
    });
  });

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const el = document.querySelector(btn.dataset.copy);
      try {
        await navigator.clipboard.writeText(el.value);
        btn.textContent = "Copied";
      } catch (e) {
        el.select();
        btn.textContent = "Press Ctrl+C";
      }
      setTimeout(() => { btn.textContent = "Copy text"; }, 2000);
    });
  });

  // Refresh the board every 30 s so it stays current, unless someone is typing.
  const page = document.body.dataset.page;
  if ((page === "board" || page === "human" || page === "runs") && document.body.dataset.frozen !== "1") {
    setInterval(() => {
      const a = document.activeElement;
      if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA")) return;
      location.reload();
    }, 30000);
  }
})();
