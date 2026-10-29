import { linkProps } from "../router";

const Review = () => <span className="confirm">Legal review needed</span>;

export function Terms() {
  return (
    <article className="panel doc">
      <h1>Terms and conditions</h1>
      <p className="meta">
        Draft, last updated 29 October 2026. These terms have not been legally reviewed or approved. The owner must review them, and
        a lawyer may need to, before any public launch.
      </p>

      <h2>1. What ScaleTrace is</h2>
      <p>
        ScaleTrace is a research prototype and developer tool. It looks for code patterns in Python and FastAPI backends that may make
        endpoints slower or use more memory as data grows. It links these patterns to endpoints in a knowledge graph and ranks likely
        root causes. It is provided for research, education and evaluation.
      </p>

      <h2>2. Appropriate use</h2>
      <ul>
        <li>Analyse or upload only code you own or have permission to analyse.</li>
        <li>Do not upload archives that contain secrets, credentials or personal data. ScaleTrace does not need them, and uploaded files are written briefly to the server's disk and kept in its memory without encryption.</li>
        <li>Do not use ScaleTrace to break the law, to infringe others' rights, or to attack or overload systems.</li>
        <li>Do not try to read files outside the folders the server makes available, or to get around its safety limits.</li>
      </ul>

      <h2>3. Your responsibility for code and permissions</h2>
      <p>
        You are responsible for having the rights and permissions needed for any code you analyse, including licence terms and your
        employer's or client's policies. You are also responsible for any decision you make based on the results.
      </p>

      <h2>4. Limitations of the results</h2>
      <ul>
        <li>Most findings come from static analysis. They are candidates that match a rule, not proven defects or observed failures.</li>
        <li>ScaleTrace does not detect all scalability problems. Several rules in its rulebook have no detector yet, and its call graph is approximate.</li>
        <li>Findings may be wrong (false positives) or incomplete (false negatives).</li>
        <li>Root-cause rankings, risk scores and GNN scores order candidates. They are not probabilities and do not prove causation.</li>
        <li>Runtime evidence reflects only the recorded workload, data sizes and environment.</li>
        <li>
          Published evaluation results are based on a small benchmark whose labels have not yet been independently verified. On that
          benchmark, the learned models did not outperform a rules-only baseline.
        </li>
      </ul>

      <h2>5. Research prototype</h2>
      <p>
        ScaleTrace is under active development. Features, outputs and formats may change or stop working without notice. It is not
        intended for safety-critical, production-critical or compliance decisions.
      </p>

      <h2>6. No warranty</h2>
      <p>
        ScaleTrace is provided "as is" and "as available", without warranties of any kind, express or implied, including
        merchantability, fitness for a particular purpose, accuracy and non-infringement, to the extent the law allows. <Review />
      </p>

      <h2>7. Limitation of liability</h2>
      <p>
        To the extent the law allows, the authors and operators are not liable for any indirect, incidental, special or consequential
        loss, or for lost profits, data or business, arising from the use of ScaleTrace or reliance on its results. Some jurisdictions
        do not allow these exclusions, so they may not apply to you. <Review />
      </p>

      <h2>8. Third-party software</h2>
      <p>
        ScaleTrace depends on open-source software that is licensed under its own terms. The project's own licence has not yet been
        set. <Review />
      </p>

      <h2>9. Changes and governing law</h2>
      <p>
        These terms may be updated, and the date at the top will change. The governing law, jurisdiction and contact address have not
        been set. <Review />
      </p>

      <p style={{ marginTop: 24 }}>
        See also the <a {...linkProps("/privacy")}>privacy policy</a>.
      </p>
    </article>
  );
}
