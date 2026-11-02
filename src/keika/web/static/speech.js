// 「話す」ボタン。端末内の音声認識が日本語で使える時だけ出す。外部へ送る認識には切り替えない。
(function () {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = document.getElementById("mic");
  const text = document.getElementById("text");
  const state = document.getElementById("mic-state");
  if (!Recognition || !mic || !text || !Recognition.available) return;
  const LOCAL = { langs: ["ja-JP"], processLocally: true };

  function ready() {
    const rec = new Recognition();
    rec.lang = "ja-JP";
    rec.continuous = true;
    rec.interimResults = true;
    rec.processLocally = true;
    let listening = false;
    let base = "";

    mic.textContent = "話す（端末内）";
    mic.hidden = false;
    mic.onclick = function () {
      if (listening) {
        rec.stop();
        return;
      }
      base = text.value ? text.value + "\n" : "";
      rec.start();
    };
    rec.onstart = function () {
      listening = true;
      mic.textContent = "止める";
      state.textContent = "聞いています…";
    };
    rec.onend = function () {
      listening = false;
      mic.textContent = "話す（端末内）";
      state.textContent = "";
    };
    rec.onerror = function (e) {
      state.textContent = "端末内の認識で聞き取れませんでした（" + e.error + "）。OS の音声入力か文字で入れてください。";
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
  }

  function offerInstall() {
    mic.textContent = "端末内の音声認識を入れる";
    mic.hidden = false;
    mic.onclick = async function () {
      state.textContent = "日本語の言語パックを入れています…";
      const ok = await Recognition.install(LOCAL).catch(function () {
        return false;
      });
      if (ok) {
        state.textContent = "";
        ready();
      } else {
        mic.hidden = true;
        state.textContent = "入れられませんでした。OS の音声入力か文字で入れてください。";
      }
    };
  }

  Recognition.available(LOCAL)
    .then(function (status) {
      if (status === "available") ready();
      else if (status === "downloadable" || status === "downloading") offerInstall();
    })
    .catch(function () {
      /* 確かめられない時はボタンを出さない */
    });
})();
