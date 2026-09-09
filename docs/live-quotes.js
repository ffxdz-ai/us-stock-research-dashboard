(function exposeLiveQuoteUtils(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.LiveQuoteUtils = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createLiveQuoteUtils() {
  "use strict";

  const SESSION_LABELS = {
    regular: "常规交易时段",
    rth: "常规交易时段",
    pre: "盘前",
    pre_market: "盘前",
    premarket: "盘前",
    after: "盘后",
    after_hours: "盘后",
    post: "盘后",
    overnight: "夜盘",
    closed: "休市",
    halted: "停牌",
    unknown: "时段待确认",
  };

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function positiveNumber(value) {
    const parsed = finiteNumber(value);
    return parsed !== null && parsed > 0 ? parsed : null;
  }

  function normalizeSymbol(value) {
    const symbol = String(value || "").trim().toUpperCase();
    return /^(?:US|HK|CN|SH|SZ)\.[A-Z0-9.-]+$/.test(symbol) ? symbol : "";
  }

  function parseZonedTime(value) {
    const text = String(value || "").trim();
    if (!text || !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) return null;
    const timestamp = Date.parse(text);
    return Number.isFinite(timestamp) ? timestamp : null;
  }

  function normalizeSession(value) {
    const key = String(value || "unknown").trim().toLowerCase().replace(/[\s-]+/g, "_");
    return {
      key: SESSION_LABELS[key] ? key : "unknown",
      label: SESSION_LABELS[key] || SESSION_LABELS.unknown,
    };
  }

  function inferCurrency(symbol, value) {
    const explicit = String(value || "").trim().toUpperCase();
    if (explicit) return explicit;
    if (symbol.startsWith("US.")) return "USD";
    if (symbol.startsWith("HK.")) return "HKD";
    if (/^(?:CN|SH|SZ)\./.test(symbol)) return "CNY";
    return "";
  }

  function selectQuotePrice(quote) {
    if (!quote || typeof quote !== "object") return null;
    for (const key of ["live_price", "current_price", "last_price", "cur_price", "price"]) {
      const price = positiveNumber(quote[key]);
      if (price !== null) return price;
    }
    return null;
  }

  function normalizeQuote(rawQuote, fallbackSymbol) {
    const quote = rawQuote && typeof rawQuote === "object" ? rawQuote : {};
    const symbol = normalizeSymbol(quote.code || quote.symbol || fallbackSymbol);
    const timestampKind = String(quote.timestamp_kind || "exchange").trim().toLowerCase();
    const isReceiptTimestamp = timestampKind.includes("push") || timestampKind.includes("receive") || timestampKind.includes("receipt");
    const quoteTime = String(
      quote.exchange_quote_time || (isReceiptTimestamp ? "" : (quote.live_quote_time || quote.quote_time || quote.update_time)) || "",
    ).trim();
    const pushReceivedAt = String(quote.push_received_at || quote.received_at || "").trim();
    const session = normalizeSession(quote.live_session || quote.session || quote.market_session);
    return {
      symbol,
      price: selectQuotePrice(quote),
      currency: inferCurrency(symbol, quote.currency),
      quoteTime,
      quoteTimestamp: parseZonedTime(quoteTime),
      pushReceivedAt,
      pushReceivedTimestamp: parseZonedTime(pushReceivedAt),
      timestampKind,
      pushConfirmed: quote.push_confirmed === true,
      session: session.key,
      sessionLabel: session.label,
      source: String(quote.source || quote.price_source || "Futu OpenD").trim() || "Futu OpenD",
      raw: quote,
    };
  }

  function normalizeSnapshot(payload, fetchedAt) {
    const top = payload && typeof payload === "object" ? payload : {};
    const snapshot = top.snapshot && typeof top.snapshot === "object" ? top.snapshot : top;
    const rawQuotes = snapshot.quotes || top.quotes || [];
    const quotes = new Map();
    if (Array.isArray(rawQuotes)) {
      rawQuotes.forEach((raw) => {
        const quote = normalizeQuote(raw);
        if (quote.symbol) quotes.set(quote.symbol, quote);
      });
    } else if (rawQuotes && typeof rawQuotes === "object") {
      Object.entries(rawQuotes).forEach(([symbol, raw]) => {
        const quote = normalizeQuote(raw, symbol);
        if (quote.symbol) quotes.set(quote.symbol, quote);
      });
    }
    const receivedAt = String(
      snapshot.received_at || top.received_at || snapshot.updated_at || top.updated_at || snapshot.generated_at || top.generated_at || "",
    ).trim();
    const connectedValue = snapshot.opend_connected ?? top.opend_connected ?? snapshot.connected ?? top.connected;
    const status = String(snapshot.status || top.status || "").trim().toLowerCase();
    const markedStale = snapshot.stale === true || top.stale === true;
    return {
      quotes,
      receivedAt,
      receivedTimestamp: parseZonedTime(receivedAt),
      fetchedAt: finiteNumber(fetchedAt) || Date.now(),
      connected: connectedValue !== false && !markedStale && !["offline", "error", "disconnected"].includes(status),
      stale: markedStale,
      status,
      message: String(snapshot.message || top.message || "").trim(),
    };
  }

  function assessQuoteFreshness(quote, snapshot, now, options) {
    const currentTime = finiteNumber(now) || Date.now();
    const config = {
      maxQuoteAgeMs: 180000,
      maxTransportAgeMs: 90000,
      futureToleranceMs: 30000,
      ...(options || {}),
    };
    if (snapshot?.stale === true) {
      return { status: "disconnected", label: "同步已过期", executable: false, reason: snapshot.message || "云端行情快照已过期" };
    }
    if (!snapshot || snapshot.connected === false) {
      return { status: "disconnected", label: "OpenD 已断开", executable: false, reason: snapshot?.message || "未收到 OpenD 连接" };
    }
    if (!quote || quote.price === null) {
      return { status: "unavailable", label: "暂无行情", executable: false, reason: "该股票尚未收到有效报价" };
    }
    if (quote.raw?.stale === true) {
      return { status: "delayed", label: "行情已延迟", executable: false, reason: "云端已将该股票报价标记为过期" };
    }
    const usesReceiptFreshness = quote.timestampKind.includes("push")
      || quote.timestampKind.includes("receive")
      || quote.timestampKind.includes("receipt");
    if (usesReceiptFreshness && quote.pushConfirmed !== true) {
      return { status: "pending", label: "待实时变动确认", executable: false, reason: "当前值可能是订阅后的首个缓存推送" };
    }
    if (snapshot.receivedTimestamp === null) {
      return { status: "unverified", label: "时效待确认", executable: false, reason: "云端接收时间缺少明确时区" };
    }
    const transportAge = currentTime - snapshot.receivedTimestamp;
    if (transportAge < -config.futureToleranceMs || transportAge > config.maxTransportAgeMs) {
      return { status: "disconnected", label: "同步已中断", executable: false, reason: "云端行情同步已超过时效", transportAgeMs: transportAge };
    }
    if (usesReceiptFreshness) {
      const pushAge = quote.pushReceivedTimestamp === null ? null : currentTime - quote.pushReceivedTimestamp;
      if (pushAge === null || pushAge < -config.futureToleranceMs || pushAge > config.maxQuoteAgeMs) {
        return { status: "delayed", label: "推送已延迟", executable: false, reason: "Futu 推送接收时间已超过时效", pushAgeMs: pushAge, transportAgeMs: transportAge };
      }
      return {
        status: "push",
        label: "Futu 推送已确认",
        executable: false,
        reason: "扩展时段缺少独立交易所成交时间，仅按推送接收时间监控",
        pushAgeMs: pushAge,
        transportAgeMs: transportAge,
      };
    }
    if (quote.quoteTimestamp === null) {
      return { status: "unverified", label: "时效待确认", executable: false, reason: "报价时间缺少明确时区", transportAgeMs: transportAge };
    }
    const quoteAge = currentTime - quote.quoteTimestamp;
    if (quoteAge < -config.futureToleranceMs) {
      return { status: "unverified", label: "时间异常", executable: false, reason: "报价时间晚于当前时间", quoteAgeMs: quoteAge, transportAgeMs: transportAge };
    }
    if (quote.session === "closed") {
      return { status: "snapshot", label: "休市快照", executable: false, reason: "休市期间仅展示最近成交价", quoteAgeMs: quoteAge, transportAgeMs: transportAge };
    }
    if (quoteAge > config.maxQuoteAgeMs) {
      return { status: "delayed", label: "行情已延迟", executable: false, reason: "实际报价时间已超过实时阈值", quoteAgeMs: quoteAge, transportAgeMs: transportAge };
    }
    return { status: "live", label: "实时", executable: true, reason: "OpenD 实时报价同步正常", quoteAgeMs: quoteAge, transportAgeMs: transportAge };
  }

  function planLevels(opportunity) {
    const item = opportunity && typeof opportunity === "object" ? opportunity : {};
    const low = positiveNumber(item.safe_entry_zone_low ?? item.safe_entry_price ?? item.entry_price);
    const high = positiveNumber(item.safe_entry_max_price ?? item.safe_entry_zone_high ?? item.safe_entry_price ?? item.entry_price);
    return {
      low,
      high,
      stop: positiveNumber(item.stop_loss),
      target: positiveNumber(item.target_price),
    };
  }

  function calculateLivePlanMetrics(opportunity, livePrice) {
    const price = positiveNumber(livePrice);
    const levels = planLevels(opportunity);
    const result = {
      price,
      ...levels,
      entryState: "unavailable",
      entryDistancePct: null,
      entryText: "稳健买点距离：计划价待结构化",
      rr: null,
      rrState: "unavailable",
      rrText: "当前 R/R：待计算",
    };
    if (price === null) return result;

    if (levels.low !== null && levels.high !== null && levels.low <= levels.high) {
      if (price < levels.low) {
        result.entryState = "below";
        result.entryDistancePct = ((levels.low - price) / levels.low) * 100;
        result.entryText = `稳健买点距离：低于计划区间下沿 ${result.entryDistancePct.toFixed(1)}%，需复核，不自动买入`;
      } else if (price <= levels.high) {
        result.entryState = "inside";
        result.entryDistancePct = 0;
        result.entryText = "稳健买点距离：已进入计划价区（仅价格条件）";
      } else {
        result.entryState = "above";
        result.entryDistancePct = ((price - levels.high) / price) * 100;
        result.entryText = `稳健买点距离：需回落 ${result.entryDistancePct.toFixed(1)}% 至计划区上沿`;
      }
    }

    if (levels.stop !== null && levels.target !== null) {
      if (price <= levels.stop) {
        result.rrState = "stop_breached";
        result.rrText = "当前 R/R：价格已触及或跌破原计划止损，必须重新复核";
      } else if (price >= levels.target) {
        result.rrState = "target_reached";
        result.rrText = "当前 R/R：价格已达到或超过原目标价，不追价";
      } else {
        result.rr = (levels.target - price) / (price - levels.stop);
        result.rrState = "valid";
        result.rrText = `当前 R/R：${result.rr.toFixed(2)}:1（按实时价监控）`;
      }
    }
    return result;
  }

  return {
    assessQuoteFreshness,
    calculateLivePlanMetrics,
    finiteNumber,
    normalizeQuote,
    normalizeSnapshot,
    normalizeSymbol,
    parseZonedTime,
    planLevels,
    selectQuotePrice,
  };
}));
