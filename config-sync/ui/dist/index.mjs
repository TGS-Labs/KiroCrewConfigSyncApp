import { jsxs as r, jsx as n, Fragment as u } from "react/jsx-runtime";
import { useAppApi as N } from "@kirocrew/app-sdk";
import { PageHeader as P } from "@kirocrew/app-sdk/ui";
import { useRef as O, useState as h, useEffect as R } from "react";
const m = {
  background: "var(--card)",
  color: "var(--card-fg)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "1rem"
}, d = {
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
function E() {
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
function x() {
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
function T({
  drift: e,
  onPushNow: i,
  pushing: t
}) {
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Local changes" }),
    /* @__PURE__ */ n("p", { style: { fontSize: "1.5rem", margin: "0.5rem 0" }, children: e ? "Yes" : "No changes yet" }),
    e ? /* @__PURE__ */ n("button", { type: "button", style: x(), onClick: i, disabled: t, children: t ? "Pushing…" : "Push now" }) : /* @__PURE__ */ n("p", { style: d, children: "Tracked tree matches the last pushed hash." })
  ] });
}
function z({
  lastPush: e,
  failure: i
}) {
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Last push" }),
    e ? /* @__PURE__ */ r(u, { children: [
      /* @__PURE__ */ n("p", { style: { margin: "0.5rem 0" }, children: _(e.time) }),
      /* @__PURE__ */ n("p", { children: /* @__PURE__ */ n("a", { href: e.pr_url, target: "_blank", rel: "noreferrer", children: e.pr_url }) }),
      /* @__PURE__ */ n("p", { style: d, children: e.merged ? "Merged" : "Open — needs merging" })
    ] }) : /* @__PURE__ */ n("p", { style: d, children: "No push yet — never pushed." }),
    i ? /* @__PURE__ */ r("p", { style: { color: "var(--danger)" }, children: [
      i.reason,
      i.time ? ` (${_(i.time)})` : null
    ] }) : null
  ] });
}
const w = 5;
function U({
  lastSeenSha: e,
  pollFailure: i,
  consecutiveFailures: t
}) {
  const l = t >= w;
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "From main" }),
    /* @__PURE__ */ n("p", { style: { fontSize: "1.1rem", margin: "0.5rem 0" }, children: i ? "Poll failing" : "Up to date" }),
    /* @__PURE__ */ r("p", { style: d, children: [
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
      l ? /* @__PURE__ */ r("p", { style: d, children: [
        "KiroCrew pauses the poll job after ",
        w,
        " ",
        "consecutive failures; re-enable config-sync/config-sync-poll on the Schedule page once the cause is fixed."
      ] }) : null
    ] }) : null
  ] });
}
const B = [
  { cls: "live_immediate", label: "live now" },
  { cls: "live_within_60s", label: "live within 60s" },
  { cls: "live_in_new_session", label: "live in a new session" },
  { cls: "live_on_next_resolution", label: "live on next resolution" }
];
function H({ lastApply: e }) {
  const i = {
    live_immediate: 0,
    live_within_60s: 0,
    live_in_new_session: 0,
    live_on_next_resolution: 0
  };
  for (const t of Object.values(e.propagation ?? {}))
    t.propagation_class !== void 0 && (i[t.propagation_class] = (i[t.propagation_class] ?? 0) + 1);
  return /* @__PURE__ */ n("div", { style: { display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.75rem" }, children: B.map(({ cls: t, label: l }) => /* @__PURE__ */ r("span", { style: E(), children: [
    l,
    /* @__PURE__ */ n("span", { children: i[t] })
  ] }, t)) });
}
function I({
  lastApply: e,
  onUndo: i,
  undoing: t
}) {
  var l, p;
  return /* @__PURE__ */ r("div", { style: m, children: [
    /* @__PURE__ */ n("h2", { style: { margin: 0, fontSize: "0.9rem" }, children: "Last apply" }),
    e ? /* @__PURE__ */ r(u, { children: [
      /* @__PURE__ */ n("p", { style: { margin: "0.5rem 0" }, children: e.outcome === "applied" ? "Applied" : `Outcome: ${e.outcome}` }),
      /* @__PURE__ */ r("p", { style: d, children: [
        "Applied sha: ",
        e.sha.slice(0, 7)
      ] }),
      e.apply_id ? /* @__PURE__ */ n(
        "button",
        {
          type: "button",
          style: x(),
          onClick: () => i(e.apply_id),
          disabled: t,
          children: t ? "Undoing…" : "Undo this apply"
        }
      ) : /* @__PURE__ */ n("p", { style: d, children: "Nothing to undo: this attempt wrote no files." }),
      /* @__PURE__ */ n(H, { lastApply: e }),
      e.paused_cron_names.length > 0 ? /* @__PURE__ */ r("div", { style: g("warn"), children: [
        /* @__PURE__ */ n("strong", { children: "Paused crons" }),
        /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: e.paused_cron_names.map((o) => {
          const a = (e.changed_commands ?? []).find((c) => c.name === o);
          return /* @__PURE__ */ r("li", { children: [
            o,
            a ? /* @__PURE__ */ r(u, { children: [
              " — ",
              /* @__PURE__ */ n("code", { children: a.command })
            ] }) : null
          ] }, o);
        }) })
      ] }) : null,
      Object.keys(e.not_applied).length > 0 ? /* @__PURE__ */ r("div", { style: g("warn"), children: [
        /* @__PURE__ */ n("strong", { children: "Not applied" }),
        /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: Object.entries(e.not_applied).map(([o, a]) => /* @__PURE__ */ r("li", { children: [
          o,
          a ? `: ${a}` : null
        ] }, o)) })
      ] }) : null,
      (() => {
        const o = new Set(
          Object.keys(e.not_applied).map((c) => c.split(":")[0])
        ), a = e.needs_credential.filter(
          (c) => !o.has(c.split(":")[0])
        );
        return a.length > 0 ? /* @__PURE__ */ r("div", { style: g("danger"), children: [
          /* @__PURE__ */ n("strong", { children: "Needs credential" }),
          /* @__PURE__ */ n("ul", { style: { margin: "0.25rem 0 0", paddingLeft: "1.25rem" }, children: a.map((c) => /* @__PURE__ */ n("li", { children: c }, c)) })
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
    ] }) : /* @__PURE__ */ n("p", { style: d, children: "Never applied yet." })
  ] });
}
const S = "/apps/config-sync/api";
function W() {
  const e = N(), i = O(e);
  i.current = e;
  const t = {
    get: (s) => i.current.get(`${S}${s}`),
    post: (s) => i.current.post(`${S}${s}`)
  }, [l, p] = h(null), [o, a] = h(null), [c, C] = h(!0), [k, v] = h(!1), [L, b] = h(!1);
  async function f() {
    try {
      const s = await t.get("/status");
      p(s), a(null);
    } catch (s) {
      a(s instanceof Error ? s.message : String(s));
    } finally {
      C(!1);
    }
  }
  R(() => {
    f();
  }, []);
  async function j() {
    v(!0);
    try {
      await t.post("/push"), await f();
    } catch (s) {
      a(s instanceof Error ? s.message : String(s));
    } finally {
      v(!1);
    }
  }
  async function $(s) {
    b(!0);
    try {
      await t.post(`/restore/${s}`), await f();
    } catch (y) {
      a(y instanceof Error ? y.message : String(y));
    } finally {
      b(!1);
    }
  }
  return /* @__PURE__ */ r(u, { children: [
    /* @__PURE__ */ n(P, { title: "Config bundle status", subtitle: "Keeps this box aligned with Kiro-Config-Bundles" }),
    /* @__PURE__ */ n("div", { className: "px-6 pb-8 overflow-y-auto flex-1 min-h-0", children: c ? /* @__PURE__ */ n("p", { style: d, children: "Loading…" }) : o ? /* @__PURE__ */ n("div", { style: m, children: /* @__PURE__ */ n("p", { style: { color: "var(--danger)" }, children: o }) }) : l ? /* @__PURE__ */ r(u, { children: [
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
            /* @__PURE__ */ n(T, { drift: l.drift, onPushNow: j, pushing: k }),
            /* @__PURE__ */ n(z, { lastPush: l.last_push, failure: l.last_push_failure }),
            /* @__PURE__ */ n(
              U,
              {
                lastSeenSha: l.last_seen_sha,
                pollFailure: l.last_poll_failure,
                consecutiveFailures: l.poll_consecutive_failures
              }
            )
          ]
        }
      ),
      /* @__PURE__ */ n(I, { lastApply: l.last_apply, onUndo: $, undoing: L })
    ] }) : null })
  ] });
}
export {
  W as default
};
