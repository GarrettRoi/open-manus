import { fetchJSON } from "@/lib/api";

export type Business = "dj_wedding" | "real_estate" | "other";
export type LeadStatus = "new" | "contacted" | "qualified" | "proposal" | "won" | "lost";
export type LeadInput = {
  name: string; email?: string; phone?: string; company?: string;
  source_id?: string; external_id?: string; business: Business; lead_type?: string;
  status: LeadStatus; assigned_agent?: string; estimated_value?: string | null;
  currency?: string; acquisition_date?: string; action_timeframe?: string;
  next_action_date?: string; wedding_date?: string; custom_fields?: Record<string, string | number | boolean | null>;
};
export type Lead = LeadInput & {
  id: string; revision: number; created_at: string; updated_at: string;
  archived: boolean; duplicate_ids?: string[];
  notes?: Array<{ id?: string; text: string; actor?: string; author?: string; created_at?: string | number }>;
  notes_total?: number; history_total?: number;
  history?: Array<{ id?: string; action?: string; actor?: string; changes?: Record<string, { old?: unknown; new?: unknown }>; fields?: string[]; created_at?: string | number; timestamp?: string | number; at?: number }>;
};
export type Source = {
  id: string; name: string; domain?: string; revision?: number; business?: Business;
  assigned_agent?: string; mapping?: Record<string, string>; enabled?: boolean;
  connected?: boolean; last_delivery_at?: string | number; last_delivery_ok?: boolean; last_error?: string; last_test_at?: string; last_test_status?: string;
  test_status?: string; instructions?: string; auth_requirements?: string; example_payload?: Record<string, unknown>;
  connection_status?: string; setup?: string; authentication?: string; example?: Record<string, unknown>;
};
export type FieldDefinition = { id: string; label: string; revision?: number; type: "string" | "number" | "boolean" | "date" | "select"; required?: boolean; options?: string[] };
export type Notification = {
  id: string; lead_id: string; recipient?: string; status: string; attempts: number;
  error?: string; created_at?: string | number; updated_at?: string | number; next_attempt_at?: number;
};
export type ListArgs = { query?: string; business?: string; status?: string; source_id?: string; assigned_agent?: string; archived?: boolean; page: number; limit: number };

/** A lead returned by the API contains immutable metadata and history. Never
 * spread that record into a form or mutation payload: schemas reject it. */
export function editableLead(lead: Lead): LeadInput {
  return {
    name: lead.name || "", email: lead.email || "", phone: lead.phone || "",
    company: lead.company || "", source_id: lead.source_id || "",
    external_id: lead.external_id || "", business: lead.business || "other",
    lead_type: lead.lead_type || "", status: lead.status || "new",
    assigned_agent: lead.assigned_agent || "", estimated_value: lead.estimated_value ?? "",
    currency: lead.currency || "USD", acquisition_date: lead.acquisition_date || "",
    action_timeframe: lead.action_timeframe || "", next_action_date: lead.next_action_date || "",
    wedding_date: lead.wedding_date || "", custom_fields: { ...lead.custom_fields },
  };
}

/** Whitelist the editable schema again at the API boundary, even if a caller
 * accidentally supplies a runtime object containing record metadata. */
export function serializeLeadInput(form: LeadInput, mode: "create" | "update", previousAmount?: string | null): LeadInput {
  const result: LeadInput = {
    name: form.name.trim(), email: form.email || "", phone: form.phone || "",
    company: form.company || "", source_id: form.source_id || "",
    external_id: form.external_id || "", business: form.business,
    lead_type: form.lead_type || "", status: form.status,
    assigned_agent: form.assigned_agent || "", currency: (form.currency || "USD").toUpperCase(),
    acquisition_date: form.acquisition_date || "", action_timeframe: form.action_timeframe || "",
    next_action_date: form.next_action_date || "", wedding_date: form.wedding_date || "",
    custom_fields: Object.fromEntries(Object.entries(form.custom_fields || {}).filter(([, value]) => value !== "" && value !== null)),
  };
  // Explicit null clears an existing amount. An unchanged empty amount is
  // omitted so ordinary edits do not require a money-clearing mutation.
  if (form.estimated_value) result.estimated_value = form.estimated_value;
  else if (mode === "update" && previousAmount) result.estimated_value = null;
  return result;
}

export class CrmError extends Error {
  code: string;
  constructor(code: string, message: string) { super(message); this.name = "CrmError"; this.code = code; }
}
export async function crm<T>(action: string, args: Record<string, unknown> = {}): Promise<T> {
  let response: { ok: true; result: T } | { ok: false; error: { code: string; message: string } };
  try {
    response = await fetchJSON<{ ok: true; result: T } | { ok: false; error: { code: string; message: string } }>(
      "/api/crm/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, args }) },
    );
  } catch (error) {
    // The authenticated shared helper throws on HTTP errors before returning
    // domain envelopes. Recover structured validation/conflict messages here.
    const text = error instanceof Error ? error.message : String(error);
    const jsonStart = text.indexOf("{");
    if (jsonStart !== -1) {
      try {
        const payload = JSON.parse(text.slice(jsonStart)) as { error?: { code?: string; message?: string } };
        if (payload.error?.code && payload.error.message) throw new CrmError(payload.error.code, payload.error.message);
      } catch (parsed) {
        if (parsed instanceof CrmError) throw parsed;
      }
    }
    throw error;
  }
  if (!response.ok) throw new CrmError(response.error.code, response.error.message);
  return response.result;
}