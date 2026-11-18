// Connectivity-probe UI plus confirmation dialogs for destructive actions.
// All dynamic values are inserted via textContent/DOM APIs (never innerHTML),
// so hostile service names cannot turn into markup under the strict CSP.
(function () {
  "use strict";

  var STATUS_LABELS = {
    running: "核验中…",
    ok_200: "200 正常",
    bad_status: "非 200",
    connect_failed: "连接失败",
    timeout: "超时",
    unprobeable: "地址不可探测",
    interrupted: "进程重启，核验中断"
  };

  var probesByService = {};
  var pollTimer = null;

  // -- confirmation dialogs (pre-existing behaviour) ----------------------

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) {
      return;
    }
    var name = form.dataset.name || "";
    if (!window.confirm(form.dataset.confirm.replace("{name}", name))) {
      event.preventDefault();
    }
  });

  // -- probe rendering -----------------------------------------------------

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function renderProbe(service, probe) {
    var box = document.querySelector(
      '[data-result-for="' + cssEscape(service) + '"]'
    );
    if (!box) {
      return; // service row disappeared (deleted) until next full reload
    }
    box.textContent = "";
    if (!probe) {
      return;
    }

    var pill = el("span", "probe-pill status-" + probe.status);
    pill.textContent = STATUS_LABELS[probe.status] || probe.status;
    box.appendChild(pill);

    if (probe.fresh === false) {
      box.appendChild(el("span", "probe-stale", "已过期"));
    }

    box.appendChild(
      el(
        "div",
        "probe-meta",
        "所测地址: " + probe.address + " · 目录版本 v" + probe.version
      )
    );

    var detail = "";
    if (probe.status_code !== null && probe.status_code !== undefined) {
      detail = "HTTP " + probe.status_code;
      if (probe.detail) {
        detail += " · " + probe.detail;
      }
    } else if (probe.detail) {
      detail = probe.detail;
    }
    if (detail) {
      var detailNode = el("div", "probe-detail", detail);
      detailNode.title = detail;
      box.appendChild(detailNode);
    }
    if (probe.finished_at) {
      box.appendChild(el("div", "probe-time", "完成于 " + probe.finished_at));
    }
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }
    return value.replace(/[^a-zA-Z0-9_.:-]/g, function (ch) {
      return "\\" + ch;
    });
  }

  function anyRunning() {
    return Object.keys(probesByService).some(function (name) {
      return probesByService[name] && probesByService[name].status === "running";
    });
  }

  function schedulePolling() {
    if (pollTimer !== null || !anyRunning()) {
      return;
    }
    pollTimer = window.setInterval(refresh, 1000);
  }

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function refresh() {
    return fetch("/api/state", { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("state HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (state) {
        probesByService = {};
        (state.probes || []).forEach(function (probe) {
          probesByService[probe.service] = probe;
          renderProbe(probe.service, probe);
        });
        if (anyRunning()) {
          schedulePolling();
        } else {
          stopPolling();
        }
      })
      .catch(function (err) {
        // Transient failure: keep polling while a probe is running.
        if (!anyRunning()) {
          stopPolling();
        }
        if (window && window.console) {
          console.warn("probe state refresh failed:", err);
        }
      });
  }

  document.addEventListener("click", function (event) {
    var button = event.target;
    if (!(button instanceof HTMLElement) || !button.classList.contains("probe-btn")) {
      return;
    }
    var service = button.dataset.service;
    if (!service) {
      return;
    }
    button.disabled = true;
    var previousText = button.textContent;
    button.textContent = "已发起";

    fetch("/api/services/" + encodeURIComponent(service) + "/probes", {
      method: "POST",
      headers: { Accept: "application/json" }
    })
      .then(function (resp) {
        return resp.json().then(function (body) {
          return { ok: resp.ok, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          var message =
            (result.body && result.body.error && result.body.error.message) ||
            "发起核验失败";
          var box = document.querySelector(
            '[data-result-for="' + cssEscape(service) + '"]'
          );
          if (box) {
            box.textContent = "";
            box.appendChild(el("span", "probe-pill status-connect_failed", message));
          }
          return;
        }
        probesByService[service] = result.body.probe;
        renderProbe(service, result.body.probe);
        schedulePolling();
      })
      .catch(function (err) {
        var box = document.querySelector(
          '[data-result-for="' + cssEscape(service) + '"]'
        );
        if (box) {
          box.textContent = "";
          box.appendChild(
            el("span", "probe-pill status-connect_failed", "请求失败: " + err)
          );
        }
      })
      .then(function () {
        button.disabled = false;
        button.textContent = previousText;
      });
  });

  // Load any persisted results (including interrupted/completed probes) now.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refresh);
  } else {
    refresh();
  }
})();
