import { jsxs as r, jsx as n, Fragment as u } from "react/jsx-runtime";
import { useAppApi as j } from "@kirocrew/app-sdk";
import { PageHeader as P } from "@kirocrew/app-sdk/ui";
import { useRef as N, useState as h, useEffect as z } from "react";
const m = {
  background: "var(--card)",
  color: "var(--card-fg)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "1rem"
}, c = {
  color: "var(--muted)"
};
function g(e) {
  const i = e === "warn" ? "--warn-subtle" : e === "danger" ? "--danger-subtle" : "--ok-subtle", t = e === "warn" ? "--warn" : e === "danger" ? "--danger" : "--ok";
  return {
    background: `var(${i})`,
    color: `var(${t})`,
    border: `1px solid var(${t})`,
    borderRadius: 6,
    padding: "0.5rem 0.75rem",
    marginTop: "0.5rem"
  };
}
function R() {
  return {
    background: "var(--accent-subtle)",
    color: "var(--accent)",
    border: "1px solid var(--border)",
    borderRadius: 999,
    padding: "0.25rem 0.75rem",
    display: "inline-flex",
    alignItems: "center",
    gap: "0.375rem",
    fontSize: "0.85rem"
  };
}
function S() {
  return {
    background: "var(--accent)",
    color: "var(--card)",
    border: "1px solid var(--border)",
    borderRadius: 6,
    padding: "0.4rem 0.9rem",
    cursor: "pointer",
    fontSize: "0.85rem"
  };
}
function _(e) {
  if (!e) return "never";
  try {
    return new Date(e).toLocaleString();
  } catch {
    return e;
  }
}
function E({
  drift: e,
  onPushNow: i,
  pushing: t
}) {
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Local changes" }),
    /* @__PURE__ */ n("p", { style: { fontSize: "1.5rem", margin: "0.5rem 0" }, children: e ? "Yes" : "No changes yet" }),
    e ? /* @__PURE__ */ n("button", { type: "button", style: S(), onClick: i, disabled: t, children: t ? "Pushing…" : "Push now" }) : /* @__PURE__ */ n("p", { style: c, children: "Tracked tree matches the last pushed hash." })
  ] });
}
function O({
  lastPush: e,
  failure: i
}) {
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Last push" }),
    e ? /* @__PURE__ */ r(u, { children: [
      /* @__PURE__ */ n("p", { style: { margin: "0.5rem 0" }, children: _(e.time) }),
      /* @__PURE__ */ n("p", { children: /* @__PURE__ */ n("a", { href: e.pr_url, target: "_blank", rel: "noreferrer", children: e.pr_url }) }),
      /* @__PURE__ */ n("p", { style: c, children: e.merged ? "Merged" : "Open — needs merging" })
    ] }) : /* @__PURE__ */ n("p", { style: c, children: "No push yet — never pushed." }),
    i ? /* @__PURE__ */ r("p", { style: { color: "var(--danger)" }, children: [
      i.reason,
      i.time ? ` (${_(i.time)})` : null
    ] }) : null
  ] });
}
function U({
  lastSeenSha: e,
  pollFailure: i,
  consecutiveFailures: t,
  pollPaused: l
}) {
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "From main" }),
    /* @__PURE__ */ n("p", { style: { fontSize: "1.1rem", margin: "0.5rem 0" }, children: l ? "Polling paused" : "Up to date" }),
    /* @__PURE__ */ r("p", { style: c, children: [
      "Last-seen sha: ",
      e ? e.slice(0, 7) : "none"
    ] }),
    i ? /* @__PURE__ */ r("div", { style: { color: "var(--danger)" }, children: [
      /* @__PURE__ */ r("p", { style: { margin: "0.25rem 0" }, children: [
        i.reason,
        i.time ? ` (${_(i.time)})` : null
      ] }),
      t > 0 ? /* @__PURE__ */ r("p", { style: { margin: "0.25rem 0" }, children: [
        t,
        " consecutive poll failure",
        t === 1 ? "" : "s"
      ] }) : null,
      l ? /* @__PURE__ */ n("p", { style: c, children: "Polling is paused after repeated failures; Push now or Undo resumes it." }) : null
    ] }) : null
  ] });
}
const T = [
  { cls: "live_immediate", label: "live now" },
  { cls: "live_within_60s", label: "live within 60s" },
  { cls: "live_in_new_session", label: "live in a new session" },
  { cls: "live_on_next_resolution", label: "live on next resolution" }
];
function I({ lastApply: e }) {
  const i = {
    live_immediate: 0,
    live_within_60s: 0,
    live_in_new_session: 0,
    live_on_next_resolution: 0
  };
  for (const t of Object.values(e.propagation ?? {}))
    t.propagation_class !== void 0 && (i[t.propagation_class] = (i[t.propagation_class] ?? 0) + 1);
  return /* @__PURE__ */ n("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.75rem" }, children: T.map(({ cls: t, label: l }) => /* @__PURE__ */ r("span", { style: R(), children: [
    l,
    /* @__PURE__ */ n("span", { children: i[t] })
  ] }, t)) });
}
function B({
  lastApply: e,
  onUndo: i,
  undoing: t
}) {
  var l, p;
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Last apply" }),
    e ? /* @__PURE__ */ r(u, { children: [
      /* @__PURE__ */ n("p", { style: { margin: "0.5rem 0" }, children: e.outcome === "applied" ? "Applied" : `Outcome: ${e.outcome}` }),
      /* @__PURE__ */ r("p", { style: c, children: [
        "Applied sha: ",
        e.sha.slice(0, 7)
      ] }),
      /* @__PURE__ */ n(
        "button",
        {
          type: "button",
          style: S(),
          onClick: () => i(e.apply_id),
          disabled: t,
          children: t ? "Undoing…" : "Undo this apply"
        }
      ),
      /* @__PURE__ */ n(I, { lastApply: e }),
      e.paused_cron_names.length > 0 ? /* @__PURE__ */ r("div", { style: g("warn"), children: [
        /* @__PURE__ */ n("strong", { children: "Paused crons" }),
        /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: e.paused_cron_names.map((o) => {
          const s = e.changed_commands.find((d) => d.name === o);
          return /* @__PURE__ */ r("li", { children: [
            o,
            s ? /* @__PURE__ */ r(u, { children: [
              " — ",
              /* @__PURE__ */ n("code", { children: s.command })
            ] }) : null
          ] }, o);
        }) })
      ] }) : null,
      Object.keys(e.not_applied).length > 0 ? /* @__PURE__ */ r("div", { style: g("warn"), children: [
        /* @__PURE__ */ n("strong", { children: "Not applied" }),
        /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: Object.entries(e.not_applied).map(([o, s]) => /* @__PURE__ */ r("li", { children: [
          o,
          s ? `: ${s}` : null
        ] }, o)) })
      ] }) : null,
      (() => {
        const o = new Set(
          Object.keys(e.not_applied).map((d) => d.split(":")[0])
        ), s = e.needs_credential.filter(
          (d) => !o.has(d.split(":")[0])
        );
        return s.length > 0 ? /* @__PURE__ */ r("div", { style: g("danger"), children: [
          /* @__PURE__ */ n("strong", { children: "Needs credential" }),
          /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: s.map((d) => /* @__PURE__ */ n("li", { children: d }, d)) })
        ] }) : null;
      })(),
      e.applied.includes("crons.json") || e.applied.includes("instances.json") || (((l = e.changed_instance_names) == null ? void 0 : l.length) ?? 0) > 0 ? /* @__PURE__ */ r("div", { style: g("ok"), children: [
        "This is a deliberate exception to instance isolation: crons.json/instances.json sync by design.",
        (((p = e.changed_instance_names) == null ? void 0 : p.length) ?? 0) > 0 ? /* @__PURE__ */ r(u, { children: [
          " ",
          "Instances affected: ",
          e.changed_instance_names.join(", "),
          "."
        ] }) : null
      ] }) : null
    ] }) : /* @__PURE__ */ n("p", { style: c, children: "Never applied yet." })
  ] });
}
const w = "/apps/config-sync/api";
function V() {
  const e = j(), i = N(e);
  i.current = e;
  const t = {
    get: (a) => i.current.get(`${w}${a}`),
    post: (a) => i.current.post(`${w}${a}`)
  }, [l, p] = h(null), [o, s] = h(null), [d, x] = h(!0), [C, v] = h(!1), [L, b] = h(!1);
  async function f() {
    try {
      const a = await t.get("/status");
      p(a), s(null);
    } catch (a) {
      s(a instanceof Error ? a.message : String(a));
    } finally {
      x(!1);
    }
  }
  z(() => {
    f();
  }, []);
  async function k() {
    v(!0);
    try {
      await t.post("/push"), await f();
    } catch (a) {
      s(a instanceof Error ? a.message : String(a));
    } finally {
      v(!1);
    }
  }
  async function $(a) {
    b(!0);
    try {
      await t.post(`/restore/${a}`), await f();
    } catch (y) {
      s(y instanceof Error ? y.message : String(y));
    } finally {
      b(!1);
    }
  }
  return /* @__PURE__ */ r(u, { children: [
    /* @__PURE__ */ n(P, { title: "Config bundle status", subtitle: "Keeps this box aligned with Kiro-Config-Bundles" }),
    /* @__PURE__ */ n("div", { className: "px-6 pb-8 overflow-y-auto flex-1 min-h-0", children: d ? /* @__PURE__ */ n("p", { style: c, children: "Loading…" }) : o ? /* @__PURE__ */ n("div", { style: m, children: /* @__PURE__ */ n("p", { style: { color: "var(--danger)" }, children: o }) }) : l ? /* @__PURE__ */ r(u, { children: [
      /* @__PURE__ */ r(
        "div",
        {
          style: {
            display: "grid",
            gap: "0.875rem",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            marginBottom: "1.5rem"
          },
          children: [
            /* @__PURE__ */ n(E, { drift: l.drift, onPushNow: k, pushing: C }),
            /* @__PURE__ */ n(O, { lastPush: l.last_push, failure: l.last_push_failure }),
            /* @__PURE__ */ n(
              U,
              {
                lastSeenSha: l.last_seen_sha,
                pollFailure: l.last_poll_failure,
                consecutiveFailures: l.poll_consecutive_failures,
                pollPaused: l.poll_paused
              }
            )
          ]
        }
      ),
      /* @__PURE__ */ n(B, { lastApply: l.last_apply, onUndo: $, undoing: L })
    ] }) : null })
  ] });
}
export {
  V as default
};
