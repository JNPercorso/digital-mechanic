// Схематичные рисунки машин парка (вид сбоку, кабина справа) в цветах темы.
// Классы: b - кузов, c - кабина, w - стекло, k - тёмное (шины, бампер), f - рама/серые детали, a - оранжевый акцент, l - линия.
(function () {
  const wheel = (x, r = 15) => `<circle cx="${x}" cy="${113 - r}" r="${r}" class="k"/><circle cx="${x}" cy="${113 - r}" r="${Math.round(r * 0.4)}" class="h"/>`;
  const arch = (x, r = 19) => `<path class="l" d="M${x - r} ${113 - 15} a${r} ${r} 0 0 1 ${r * 2} 0"/>`;
  // Кабина МАЗ (бескапотная), правый край x0+w
  const mazCab = (x0, top = 22, w = 64) => `<path class="c" d="M${x0} ${top}h${w - 6}l6 6v58h-${w}z"/><rect class="w" x="${x0 + w - 34}" y="${top + 8}" width="26" height="22" rx="2"/><path class="l" d="M${x0 + 12} ${top + 8}v54"/><rect class="a" x="${x0 + w - 5}" y="${top + 48}" width="5" height="6"/><rect class="k" x="${x0 + w - 8}" y="84" width="12" height="8" rx="2"/>`;
  // Кабина Урал (капотная): кабина + длинный капот
  const uralCab = x0 => `<path class="c" d="M${x0} 32h40v52h-40z"/><rect class="w" x="${x0 + 10}" y="38" width="24" height="16" rx="2"/><path class="b" d="M${x0 + 40} 50h46l12 8v26h-58z"/><path class="l" d="M${x0 + 90} 60v18M${x0 + 94} 60v18"/><rect class="a" x="${x0 + 92}" y="52" width="6" height="5"/><rect class="k" x="${x0 + 90}" y="84" width="16" height="8" rx="2"/>`;
  const frame = (x0, x1) => `<rect class="f" x="${x0}" y="84" width="${x1 - x0}" height="6"/>`;
  const S = {
    'МАЗ-5440': frame(18, 296) + `<rect class="k" x="56" y="76" width="54" height="8" rx="2"/><rect class="f" x="168" y="78" width="36" height="14" rx="3"/><rect class="f" x="212" y="8" width="5" height="70"/>` +
      `<path class="b" d="M226 22l8-14h46l6 14z"/><path class="c" d="M222 22h70l14 34v30h-84z"/><path class="w" d="M266 30h22l11 24h-33z"/><path class="l" d="M244 32v50"/><rect class="a" x="226" y="70" width="30" height="4"/><rect class="a" x="300" y="66" width="6" height="6"/><rect class="k" x="296" y="84" width="14" height="8" rx="2"/>` +
      arch(264) + wheel(66) + wheel(102) + wheel(264),
    'МАЗ-5337': frame(14, 300) + `<rect class="b" x="14" y="40" width="200" height="44" rx="2"/><path class="l" d="M14 54h200M14 68h200M64 40v44M114 40v44M164 40v44"/><rect class="a" x="22" y="76" width="34" height="4"/><rect class="f" x="160" y="88" width="34" height="10" rx="2"/>` +
      mazCab(224) + arch(262) + wheel(66) + wheel(262),
    'МАЗ-54331': frame(16, 300) + `<rect class="k" x="40" y="76" width="56" height="8" rx="2"/><rect class="f" x="140" y="78" width="40" height="14" rx="3"/><rect class="f" x="214" y="18" width="5" height="62"/>` +
      mazCab(224) + arch(72) + arch(262) + wheel(72) + wheel(262),
    'МАЗ-5551': frame(22, 300) + `<path class="b" d="M28 44h170l18-16h6v56h-194z"/><path class="l" d="M28 60h188M28 72h188"/><rect class="a" x="34" y="76" width="34" height="4"/>` +
      mazCab(232, 26, 58) + arch(70) + arch(266) + wheel(70) + wheel(266),
    'ПАЗ-3205': `<path class="b" d="M8 32q0-12 12-12h292q14 0 18 14l6 40v20h-328z"/><rect class="f" x="80" y="15" width="28" height="5" rx="2"/><rect class="f" x="200" y="15" width="28" height="5" rx="2"/>` +
      `<rect class="c" x="16" y="30" width="24" height="64"/><path class="l" d="M28 30v64"/><rect class="c" x="252" y="30" width="24" height="64"/><path class="l" d="M264 30v64"/>` +
      [48, 98, 148, 198].map(x => `<rect class="w" x="${x}" y="30" width="44" height="24" rx="2"/>`).join('') + `<path class="w" d="M284 30h28l10 26h-38z"/>` +
      `<rect class="a" x="44" y="66" width="204" height="4"/><rect class="a" x="326" y="72" width="6" height="6"/><rect class="k" x="318" y="88" width="18" height="7" rx="2"/>` +
      arch(70) + arch(290) + wheel(70) + wheel(290),
    'ПАЗ-3206': `<path class="b" d="M10 28q0-12 12-12h290q14 0 18 14l6 40v18h-326z"/><rect class="f" x="120" y="11" width="28" height="5" rx="2"/>` +
      `<rect class="c" x="18" y="26" width="26" height="62"/><path class="l" d="M31 26v62"/><rect class="c" x="236" y="26" width="26" height="62"/><path class="l" d="M249 26v62"/>` +
      [52, 122].map(x => `<rect class="w" x="${x}" y="26" width="62" height="24" rx="2"/>`).join('') + `<rect class="w" x="192" y="26" width="36" height="24" rx="2"/><path class="w" d="M270 26h42l10 26h-52z"/>` +
      `<rect class="a" x="52" y="62" width="176" height="4"/><rect class="a" x="328" y="66" width="6" height="6"/><rect class="k" x="318" y="84" width="18" height="7" rx="2"/>` +
      arch(78, 21) + arch(288, 21) + wheel(78, 17) + wheel(288, 17),
    'УАЗ Патриот': `<circle cx="22" cy="62" r="13" class="k"/><circle cx="22" cy="62" r="5" class="h"/><path class="b" d="M26 52L46 22H204L236 48L308 54Q322 56 322 68V90H26Z"/><path class="l" d="M46 18H200"/>` +
      `<path class="w" d="M52 28h44v20H40z"/><rect class="w" x="102" y="28" width="48" height="20" rx="1"/><path class="w" d="M156 28h44l26 20h-70z"/><path class="l" d="M100 28v58M154 28v58M228 50v36"/>` +
      `<rect class="a" x="310" y="60" width="8" height="6"/><rect class="a" x="30" y="58" width="5" height="10"/><rect class="k" x="24" y="86" width="300" height="5" rx="2"/>` +
      arch(84, 20) + arch(268, 20) + wheel(84, 16) + wheel(268, 16),
    'Урал-4320-10': frame(14, 312) + `<path class="b" d="M14 28q0-10 10-10h172q10 0 10 10v44h-192z"/><path class="l" d="M60 18v54M110 18v54M160 18v54"/><rect class="f" x="14" y="72" width="192" height="12"/><rect class="a" x="20" y="75" width="30" height="4"/>` +
      uralCab(212) + arch(282) + wheel(58) + wheel(96) + wheel(282),
    'Урал-43206': frame(14, 316) + `<path class="b" d="M14 30q0-10 10-10h152q10 0 10 10v42h-172z"/><path class="l" d="M60 20v52M110 20v52M150 20v52"/><rect class="f" x="14" y="72" width="172" height="12"/><rect class="a" x="20" y="75" width="30" height="4"/>` +
      `<path class="c" d="M192 26h66l4 4v54h-70z"/><rect class="w" x="200" y="34" width="22" height="18" rx="2"/><rect class="w" x="230" y="34" width="24" height="18" rx="2"/><path class="l" d="M226 30v54"/>` +
      `<path class="b" d="M262 48h40q12 2 14 12v24h-54z"/><rect class="a" x="306" y="56" width="8" height="6"/><rect class="k" x="304" y="84" width="16" height="8" rx="2"/>` +
      arch(64) + arch(290) + wheel(64) + wheel(290),
    'Урал-5557-10': frame(14, 312) + `<rect class="b" x="14" y="24" width="192" height="58" rx="2"/><path class="l" d="M14 38h192M14 52h192M14 66h192"/><path class="f" d="M24 24l24-8h124l24 8z"/><rect class="a" x="20" y="74" width="30" height="4"/>` +
      uralCab(212) + arch(282) + wheel(58) + wheel(96) + wheel(282) + [58, 96, 282].map(x => `<circle cx="${x}" cy="98" r="2.5" class="a"/>`).join(''),
  };
  window.truckSVG = v => {
    const key = S[v.model] ? v.model : (v.make === 'МАЗ' ? 'МАЗ-5440' : null); // у МАЗ с ЯМЗ-536 модель в данных не указана
    return key ? `<svg class="truck" viewBox="0 0 340 120" aria-label="${key}">${S[key]}</svg>` : null;
  };
  window.TRUCK_MODELS = Object.keys(S);
})();
