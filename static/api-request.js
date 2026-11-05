/* Shared fetch boundary for the original scan/batch/balance screen. */
(function (root) {
  'use strict';
  // One True API read can take 30 s after a connection attempt of up to 10 s.
  // The browser must allow the backend to return its categorized error.
  const DEFAULT_TIMEOUT_MS = 45000;
  async function request(url, options = {}) {
    const {timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchOptions} = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
      response = await fetch(url, {...fetchOptions, signal: controller.signal});
      let data;
      try {
        data = await response.json();
      } catch (error) {
        if (controller.signal.aborted) throw error;
        return {ok: false, kind: 'server', status: response.status, data: null};
      }
      if (response.ok && data !== null && typeof data === 'object') {
        return {ok: true, kind: 'ok', status: response.status, data};
      }
      return {ok: false, kind: response.status >= 400 && response.status < 500 ? 'biz' : 'server',
        status: response.status, data};
    } catch (error) {
      return {ok: false, kind: controller.signal.aborted ? 'timeout' : 'network',
        status: response?.status || 0, data: null};
    } finally {
      clearTimeout(timer);
    }
  }
  root.GisApi = {request};
})(globalThis);
