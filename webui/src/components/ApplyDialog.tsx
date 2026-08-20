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

          <span className="lbl block">{t("ap_seq")}</span>
          <ol className="planseq">
            {steps.map((s, i) => (
              <li key={i} className={s.key === "ap_step_stop_out" || s.key === "ap_step_start_out" ? "power" : ""}>
                {t(s.key, ...s.args)}
              </li>
            ))}
          </ol>

          <div className="badges">
            <span className="pill warn">W_DRY_RUN</span>
            {plan.interruptsStream && <span className="pill warn">stream break</span>}
            {plan.restartOutput && <span className="pill warn">W_DUT_POWER_TRANSIENT</span>}
            {(plan.stopOutputFirst || plan.restartOutput) && (
              <span className="pill bad">W_STATE_UNVERIFIED</span>
            )}
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
