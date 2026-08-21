import { planSteps, type ApplyPlan } from "../data/source";
import { useI18n } from "../i18n";
import { Icon } from "./Icons";

interface ApplyDialogProps {
  plan: ApplyPlan | null;
  onCancel: () => void;
  /** `restartOutput` decides whether VOUT comes back once the settings land. */
  onApply: (restartOutput: boolean) => void;
}

/**
 * The gate every configuration change passes through.
 *
 * Nothing has reached the wire when this is on screen: it shows the projected
 * before/after, the warnings the operation carries, and the exact order the
 * steps will run in. That order is the point. With VOUT live there is no
 * "just apply" — changing the source voltage under load changes what the DUT
 * receives, so the choice offered is between leaving the output down and
 * bringing it back afterwards, never between dropping it and not.
 */
export function ApplyDialog({ plan, onCancel, onApply }: ApplyDialogProps) {
  const { t } = useI18n();
  if (!plan) return null;

  const steps = planSteps(plan);
  const live = plan.stopOutputFirst;

  return (
    <div
      className="scrim on"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-label={t("ap_title")}>
        <header>
          <h2>{t("ap_title")}</h2>
        </header>

        <div className="mbody">
          <p className="note" dangerouslySetInnerHTML={{ __html: t("md_dryrun") }} />

          <div className="statechange">
            <div className="tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("md_field")}</th>
                    <th>{t("md_before")}</th>
                    <th>{t("md_after")}</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.diff.map((d) => (
                    <tr key={d.field}>
                      <td>{d.field}</td>
                      <td>{d.from}</td>
                      <td style={{ color: "var(--trace)", fontWeight: 600 }}>{d.to}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Not a projection this console computed: the device was asked, and
              every step came back `applied: false`. */}
          {plan.preview && (
            <p
              className="note"
              dangerouslySetInnerHTML={{
                __html: t("ap_dryrun_confirmed", plan.preview.steps.length),
              }}
            />
          )}

          <span className="lbl block">{t("ap_seq")}</span>
          <ol className="planseq">
            {steps.map((s, i) => (
              <li key={i} className={s.key === "ap_step_stop_out" || s.key === "ap_step_start_out" ? "power" : ""}>
                {t(s.key, ...s.args)}
              </li>
            ))}
          </ol>

          {/* The warnings the device itself raised, not a guess at which ones
              it might. `md_dryrun` above promises no byte reached the wire,
              and this is where that promise is kept: these came back from a
              `dry_run=True` round trip that returned `applied: false`. */}
          <div className="badges">
            {(plan.preview
              ? [...new Set(plan.preview.steps.flatMap((step) => step.warnings))]
              : ["W_DRY_RUN"]
            ).map((code) => (
              <span key={code} className={code === "W_STATE_UNVERIFIED" ? "pill bad" : "pill warn"}>
                {code}
              </span>
            ))}
            {plan.interruptsStream && <span className="pill warn">stream break</span>}
          </div>

          {live && <p className="note badish">{t("ap_live_note")}</p>}
          {plan.interruptsStream && (
            <p className="note warnish" dangerouslySetInnerHTML={{ __html: t("md_int_note") }} />
          )}
        </div>

        <footer>
          <button type="button" className="btn" onClick={onCancel}>
            {t("ap_cancel")}
          </button>
          {live ? (
            <>
              <button type="button" className="btn" onClick={() => onApply(false)}>
                {t("ap_stop_apply")}
              </button>
              <button type="button" className="btn primary" onClick={() => onApply(true)}>
                <Icon name="record" />
                <span>{t("ap_apply_restart")}</span>
              </button>
            </>
          ) : (
            <button type="button" className="btn primary" onClick={() => onApply(false)}>
              {t("ap_apply")}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}
