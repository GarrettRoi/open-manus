import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import CrmPage from "./CrmPage";

describe("Lead desk initial render", () => {
  it("retains existing workspace controls and adds a cross-lead activity section", () => {
    const html = renderToStaticMarkup(createElement(CrmPage));
    for (const text of ["Lead desk", "Refresh", "Export", "New lead", "Leads", "Sources &amp; routing", "Custom fields", "Delivery health", "Recent activity"]) {
      expect(html).toContain(text);
    }
  });
  it("makes the global/filtered distinction and calendar semantics explicit", () => {
    const html = renderToStaticMarkup(createElement(CrmPage));
    expect(html).toContain("GLOBAL TOTALS");
    expect(html).toContain("NOT FILTERED");
    expect(html).toContain("FILTERED PIPELINE");
    expect(html).toContain("CRM calendar");
    expect(html).toContain("on or before today");
    expect(html).toContain("exclude won, lost, archived and undated leads");
  });
  it("shows loading states rather than fabricated counts, empty results or stale rows", () => {
    const html = renderToStaticMarkup(createElement(CrmPage));
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain("Awaiting calendar from server");
    expect(html).toContain("Name, email, phone, company");
    expect(html).toContain("Ready now");
    expect(html).toContain("Overdue");
    expect(html).toContain("Next action · earliest first");
    expect(html).not.toContain("No leads in this view");
    expect(html).not.toContain("crm-table");
    expect(html).not.toMatch(/\p{Extended_Pictographic}/u);
  });
});