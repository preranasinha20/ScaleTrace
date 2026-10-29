import { linkProps } from "../router";

const Confirm = () => <span className="confirm">Owner to confirm</span>;

export function Privacy() {
  return (
    <article className="panel doc">
      <h1>Privacy policy</h1>
      <p className="meta">Draft, last updated 29 October 2026. This draft describes how the current code behaves. It has not had legal review, which may be required before any public launch.</p>

      <h2>Who this applies to</h2>
      <p>
        ScaleTrace is a research prototype. Today it runs on the computer of the person who starts it, and the API listens only on
        that computer (127.0.0.1) by default. No hosted public version is operated yet. If one is launched, the operator, contact
        address and hosting provider must be added here. <Confirm />
      </p>

      <h2>What you provide</h2>
      <ul>
        <li>Your choice of project folder, picked from the folders the server lists inside its allowed root folder.</li>
        <li>An optional endpoint name you type, for example <span className="mono">GET /orders</span>.</li>
        <li>Your choice of ranking method and whether to use a recorded runtime report.</li>
        <li>Optionally, a ZIP archive of a project that you upload, and its file name.</li>
      </ul>
      <p>There are no accounts, sign-in or contact forms.</p>

      <h2>How source code is processed</h2>
      <ul>
        <li>The server reads the Python files of the selected folder and parses them. It never imports, installs or runs that code.</li>
        <li>Findings, graph data and short source excerpts around each finding are sent from the server to your browser.</li>
        <li>The web interface cannot run a runtime workload. It can only read a runtime report that someone already recorded on that machine.</li>
        <li>
          For an uploaded ZIP, the server extracts only Python files and a few configuration files (such as pyproject.toml and
          requirements files) into a private temporary folder, parses them, and deletes the folder before it answers. Other files in the
          archive are not unpacked. Nothing from the archive is imported, installed or run, and runtime reports inside it are ignored.
        </li>
      </ul>

      <h2>What is stored</h2>
      <ul>
        <li>The server keeps the results of up to 16 recent analyses in memory to respond faster. They are cleared when the server stops. The web API writes no analysis results to disk.</li>
        <li>
          For an upload, the server keeps the parsed code, the findings and short source excerpts around each finding in memory, for
          30 minutes by default (set by the operator), until you select Discard, until newer uploads replace it (8 are kept by default),
          or until the server stops. The upload's identifier is a random token; anyone who has it can read those results until then.
          The uploaded archive itself is kept in memory only while it is being processed.
        </li>
        <li>Exported files are generated on request and saved only where your browser saves downloads.</li>
        <li>The web interface does not use cookies, local storage or analytics.</li>
        <li>For a hosted deployment, retention by the hosting provider is unknown. <Confirm /></li>
      </ul>

      <h2>Logs</h2>
      <p>
        The API server (uvicorn) prints a line for each request to its console, including the client IP address, the method and the
        path. Source-excerpt requests include the project and file name in the path. These lines are not saved to a file unless the
        person running the server redirects them. A hosting provider may keep its own access logs. <Confirm />
      </p>

      <h2>Third parties</h2>
      <ul>
        <li>The web interface loads no external fonts, scripts, trackers or analytics, and sends your data to no third-party service.</li>
        <li>The exported knowledge graph HTML file, and the graph tab of the older Streamlit interface, load the vis-network library from unpkg.com when opened. Opening them lets that CDN see your IP address.</li>
        <li>The Streamlit fallback is configured with usage statistics turned off.</li>
      </ul>

      <h2>Your controls</h2>
      <ul>
        <li>Choose which folders the server can see by setting <span className="mono">SCALETRACE_ALLOWED_ROOT</span>.</li>
        <li>Stop the server to clear its in-memory results.</li>
        <li>Delete exported files from your device at any time.</li>
      </ul>

      <h2>Contact</h2>
      <p>Contact details for privacy questions have not been set. <Confirm /></p>

      <p style={{ marginTop: 24 }}>
        See also the <a {...linkProps("/terms")}>terms and conditions</a>.
      </p>
    </article>
  );
}
