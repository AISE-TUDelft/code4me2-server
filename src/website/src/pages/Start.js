import React from "react";
import { Link } from "react-router-dom";
import ThemeToggle from "../components/common/ThemeToggle";
import "./Start.css";

const Start = ({ isAuthenticated }) => {
  return (
    <div className="start-container">
      <div className="bg-decor" aria-hidden="true"></div>
      <header className="start-header glass">
        <div className="brand">
          <img src="/logo512.png" alt="Code4me2 logo" className="brand-logo" />
          <h1>Code4me2</h1>
        </div>
        <div className="header-actions">
          <nav className="start-nav">
            {isAuthenticated ? (
              <Link className="btn primary" to="/dashboard">Go to Dashboard</Link>
            ) : (
              <>
                <Link className="btn ghost" to="/login">Log in</Link>
                <Link className="btn primary" to="/signup">Sign up</Link>
              </>
            )}
          </nav>
          <ThemeToggle />
        </div>
      </header>

      <main className="start-main">
        <section className="hero">
          <span className="kicker">Open research platform</span>
          <h2>
            Modern AI-powered code completion for real developer workflows
          </h2>
          <p>
            Code4me2 combines an analytics-enabled backend with IDE integrations to
            explore, evaluate, and improve AI code completion in real projects.
          </p>
          {!isAuthenticated && (
            <div className="cta">
              <Link className="btn primary large" to="/signup">Create an account</Link>
              <Link className="btn large" to="/login">I already have an account</Link>
            </div>
          )}
        </section>

        <div className="info-grid">
          <section className="links card">
            <h3>Resources</h3>
            <ul>
              <li>
                <a href="https://github.com/AISE-TUDelft/code4me2" target="_blank" rel="noreferrer">
                  Code4me2 JetBrains Plugin (Client)
                </a>
              </li>
              <li>
                <a href="https://github.com/AISE-TUDelft/code4me2-server" target="_blank" rel="noreferrer">
                  Code4me2 Server (Backend + Web)
                </a>
              </li>
              <li>
                <a href="https://www.youtube.com/watch?v=8uCJpgCCwFA" target="_blank" rel="noreferrer">
                  (video demo of the tool with explanation)
                </a>
              </li>
              <li>
                <a href="https://plugins.jetbrains.com/vendor/code4me-team" target="_blank" rel="noreferrer">
                  JetBrains Marketplace — Code4me Team
                </a>
              </li>
            </ul>
          </section>

          <section className="about card">
            <h3>What you can do</h3>
            <ul>
              <li>Authenticate with email/password or Google</li>
              <li>Explore analytics dashboards for usage and model performance</li>
              <li>Participate in studies and A/B experiments</li>
              <li>Integrate with the JetBrains plugin to collect telemetry</li>
            </ul>
          </section>
        </div>
        <section className="features-island card">
          <h3>Platform features</h3>
          <div className="features-grid">
            <div className="feature">
              <div className="feat-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                  <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" />
                </svg>
              </div>
              <div className="feat-content">
                <h4>Real-time AI completions</h4>
                <p>Low-latency, context-aware code suggestions with streaming delivery.</p>
              </div>
            </div>

            <div className="feature">
              <div className="feat-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                  <path d="M7 2h10v6h5v6h-5v6H7v-6H2V8h5V2z" />
                </svg>
              </div>
              <div className="feat-content">
                <h4>JetBrains IDE integration</h4>
                <p>Plugin for JetBrains IDEs with telemetry and study controls.</p>
              </div>
            </div>

            <div className="feature">
              <div className="feat-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                  <rect x="3" y="10" width="4" height="10" rx="1" />
                  <rect x="10" y="6" width="4" height="14" rx="1" />
                  <rect x="17" y="12" width="4" height="8" rx="1" />
                </svg>
              </div>
              <div className="feat-content">
                <h4>Advanced analytics</h4>
                <p>Usage metrics, performance insights, and acceptance trends.</p>
              </div>
            </div>

            <div className="feature">
              <div className="feat-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                  <path d="M5 3h14v7a7 7 0 1 1-14 0V3zm7 10a3 3 0 0 0 3-3H9a3 3 0 0 0 3 3z" />
                </svg>
              </div>
              <div className="feat-content">
                <h4>A/B experiments</h4>
                <p>Run controlled studies and compare configurations scientifically.</p>
              </div>
            </div>

            <div className="feature">
              <div className="feat-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                  <path d="M12 2l8 4v6c0 5-3.5 9.5-8 10-4.5-.5-8-5-8-10V6l8-4zm0 6a4 4 0 0 0-4 4c0 2.2 1.8 5 4 6 2.2-1 4-3.8 4-6a4 4 0 0 0-4-4z" />
                </svg>
              </div>
              <div className="feat-content">
                <h4>Privacy-first</h4>
                <p>Robust session management, secret detection, and data controls.</p>
              </div>
            </div>

            <div className="feature">
              <div className="feat-icon" aria-hidden="true">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                  <path d="M10 2h4v4h-4V2zm-2 4H4v4h4V6zm12 0h-4v4h4V6zM4 14h4v4H4v-4zm6 0h4v4h-4v-4zm10 0h-4v4h4v-4z" />
                </svg>
              </div>
              <div className="feat-content">
                <h4>Admin & study tools</h4>
                <p>Manage users, datasets, and experiments from a unified dashboard.</p>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="start-footer glass">
        <small>© {new Date().getFullYear()} Code4me2 — AISE, TU Delft</small>
      </footer>
    </div>
  );
};

export default Start;
