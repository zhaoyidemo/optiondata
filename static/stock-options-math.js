(function (root) {
  'use strict';
  const valid = v => typeof v === 'number' && Number.isFinite(v);
  function calculate(row, spot, fee, now = Date.now()) {
    const days = valid(row.expiresAt) ? (row.expiresAt - now) / 86400000 : null;
    const base = row.type === 'PUT' ? row.strike : spot;
    const eligible = !row.adjusted && !row.quoteMissing && valid(days) && days > 0 &&
      valid(row.bid) && row.bid > 0 && valid(row.bidSize) && row.bidSize > 0 &&
      valid(row.ask) && row.ask >= row.bid && valid(row.multiplier) && row.multiplier > 0 &&
      valid(row.strike) && row.strike > 0 && valid(base) && base > 0 && valid(fee) && fee >= 0;
    const otm = valid(spot) && spot > 0 && valid(row.strike)
      ? (row.type === 'PUT' ? spot - row.strike : row.strike - spot) / spot : null;
    if (!eligible) return { eligible: false, days, otm, premium: null, net: null,
      capital: null, yield: null, apr: null, breakeven: null, assignedReturn: null };
    const premium = row.bid * row.multiplier;
    const net = premium - fee;
    const capital = base * row.multiplier;
    return { eligible, days, otm, premium, net, capital, yield: net / capital,
      // Do not annualize <1-day expiry: a few remaining minutes create misleading rankings.
      apr: days >= 1 ? net / capital * 365 / days : null,
      breakeven: base - net / row.multiplier,
      assignedReturn: row.type === 'CALL' ?
        ((row.strike - spot) * row.multiplier + net) / capital : null };
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = { calculate };
  else root.EquityMath = { calculate };
})(typeof window !== 'undefined' ? window : globalThis);
