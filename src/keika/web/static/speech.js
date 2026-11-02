// 音声入力。ブラウザの Web Speech API を使う。対応していないブラウザではボタンを出さない。
(function () {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = document.getElementById("mic");
  const text = document.getElementById("text");
  const state = document.getElementById("mic-state");
  if (!Recognition || !mic || !text) return;

  const rec = new Recognition();
  rec.lang = "ja-JP";
  rec.continuous = true;
  rec.interimResults = true;
  try {
    rec.processLocally = true; // 端末の中で処理できるブラウザではそうする
  } catch (e) {
    /* 使えないブラウザでは無視する */
  }

  let listening = false;
  let base = "";

  mic.hidden = false;
  mic.addEventListener("click", function () {
    if (listening) {
      rec.stop();
      return;
    }
    base = text.value ? text.value + "\n" : "";
    rec.start();
  });
  rec.onstart = function () {
    listening = true;
    mic.textContent = "止める";
    state.textContent = "聞いています…";
  };
  rec.onend = function () {
    listening = false;
    mic.textContent = "話す";
    state.textContent = "";
  };
  rec.onerror = function (e) {
    if (e.error === "language-not-supported" && rec.processLocally) {
      // 端末の中で処理できない時は、送信先を案内した上で通常の認識に戻す
      rec.processLocally = false;
      state.textContent = "端末の中では処理できないので、ブラウザの音声認識サービスを使います。もう一度押してください。";
      return;
    }
    state.textContent = "音声を聞き取れませんでした（" + e.error + "）";
  };
  rec.onresult = function (event) {
    let finals = "";
    let interim = "";
    for (let i = 0; i < event.results.length; i++) {
      const piece = event.results[i][0].transcript;
      if (event.results[i].isFinal) finals += piece;
      else interim += piece;
    }
    text.value = base + finals + interim;
  };
})();
