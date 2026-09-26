import React, { useState, useEffect } from "react";
import Auth from "./components/auth/Auth";
import Dashboard from "./pages/Dashboard";
import ResearchStudies from "./pages/research/ResearchStudies";
import ResearchStudyEditor from "./pages/research/ResearchStudyEditor";
import ResearchJoin from "./pages/research/ResearchJoin";
import AppShell from "./components/layout/AppShell";
import "./App.css";
import { GoogleOAuthProvider } from "@react-oauth/google";
import { ThemeProvider } from "./context/ThemeContext";
import { getCurrentUser, logoutUser } from "./utils/api";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  Outlet,
  useNavigate,
} from "react-router-dom";
import Start from "./pages/Start";

const USER_STORAGE_KEY = "user";
const RESEARCH_JOIN_INTENT_KEY = "code4me.research.join.intent";

// The session lives in the httpOnly auth cookie. Only non-secret profile
// fields are cached locally: the login response also carries the live auth
// token (and a masked password), which must never be copied into
// localStorage where any script on the origin could read it.
// React Router 6.26 warns about its v7 behaviours until they are opted in.
const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const PUBLIC_USER_FIELDS = ["user_id", "email", "name", "is_admin", "can_research", "verified", "joined_at"];

export const sanitizeUser = (user) => {
  if (!user || typeof user !== "object") return null;
  return PUBLIC_USER_FIELDS.reduce((safe, key) => {
    if (user[key] !== undefined) safe[key] = user[key];
    return safe;
  }, {});
};

const rememberUser = (user) => {
  try {
    const safe = sanitizeUser(user);
    if (safe) localStorage.setItem(USER_STORAGE_KEY, JSON.stringify({ user: safe }));
    else localStorage.removeItem(USER_STORAGE_KEY);
  } catch (_) {
    // Storage can be unavailable (private mode); the cookie session still works.
  }
};

const forgetUser = () => {
  try {
    localStorage.removeItem(USER_STORAGE_KEY);
  } catch (_) {
    // ignore
  }
};

function App() {
  const [user, setUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  // Resolve the session from the server-side auth cookie on load.
  useEffect(() => {
    const checkAuthStatus = async () => {
      try {
        const response = await getCurrentUser();
        if (response.ok) {
          setUser(response.user);
          rememberUser(response.user);
        } else {
          setUser(null);
          forgetUser();
        }
      } catch (error) {
        console.error("Error checking auth status:", error);
        setUser(null);
        forgetUser();
      } finally {
        setIsLoading(false);
      }
    };
    checkAuthStatus();
  }, []);

  const handleAuthenticated = (userData) => {
    setUser(userData.user);
    rememberUser(userData.user);
  };

  const handleLogout = async () => {
    try {
      const logoutResponse = await logoutUser();
      if (!logoutResponse.ok) {
        console.warn("Logout API failed, but continuing with local cleanup");
      }
    } catch (error) {
      console.error("Error during logout API call:", error);
    }
    setUser(null);
    forgetUser();
    sessionStorage.clear();
  };

  const ProtectedRoute = ({ children }) => {
    if (!user) return <Navigate to="/login" replace />;
    return children;
  };

  // Every signed-in page renders inside the same shell (header + sidebar).
  const ShellLayout = () => {
    const navigate = useNavigate();
    const onLogoutWrapped = async () => {
      await handleLogout();
      navigate("/", { replace: true });
    };
    return (
      <AppShell user={user} onLogout={onLogoutWrapped}>
        <Outlet />
      </AppShell>
    );
  };

  // The join page must stay reachable without a session: a visitor enters a
  // join code first and is sent to login (intent preserved) only when the
  // server asks for authentication.
  const PublicJoinLayout = () => (
    <div className="shell public-shell">
      <main className="public-shell-main">
        <div className="public-shell-brand">
          <span className="shell-logo" aria-hidden="true">
            C4
          </span>
          <span className="shell-brand-name">Code4Me research</span>
        </div>
        <Outlet />
      </main>
    </div>
  );

  const JoinLayout = () => (user ? <ShellLayout /> : <PublicJoinLayout />);

  const AuthPage = ({ mode }) => {
    const navigate = useNavigate();
    const onAuth = (userData) => {
      handleAuthenticated(userData);
      const pendingJoin = sessionStorage.getItem(RESEARCH_JOIN_INTENT_KEY);
      navigate(pendingJoin ? "/research/join" : "/dashboard", { replace: true });
    };
    return <Auth onAuthenticated={onAuth} initialMode={mode} />;
  };

  if (isLoading) {
    return (
      <GoogleOAuthProvider clientId={process.env.REACT_APP_GOOGLE_CLIENT_ID}>
        <ThemeProvider>
          <div className="App">
            <div className="app-loading">
              <div className="spinner"></div>
              <p>Loading...</p>
            </div>
          </div>
        </ThemeProvider>
      </GoogleOAuthProvider>
    );
  }

  return (
    <GoogleOAuthProvider clientId={process.env.REACT_APP_GOOGLE_CLIENT_ID}>
      <ThemeProvider>
        <BrowserRouter future={ROUTER_FUTURE}>
          <div className="App">
            <Routes>
              <Route
                path="/"
                element={user ? <Navigate to="/dashboard" replace /> : <Start isAuthenticated={!!user} />}
              />
              <Route path="/login" element={<AuthPage mode="login" />} />
              <Route path="/signup" element={<AuthPage mode="signup" />} />
              <Route
                element={
                  <ProtectedRoute>
                    <ShellLayout />
                  </ProtectedRoute>
                }
              >
                <Route path="/dashboard" element={<Dashboard user={user} />} />
                <Route path="/research" element={<Navigate to="/research/studies" replace />} />
                <Route path="/research/studies" element={<ResearchStudies />} />
                <Route path="/research/studies/:studyId" element={<ResearchStudies />} />
                <Route path="/research/studies/:studyId/editor" element={<ResearchStudyEditor />} />
                <Route path="/research/my-studies" element={<ResearchJoin user={user} />} />
              </Route>
              <Route path="/research/join" element={<JoinLayout />}>
                <Route index element={<ResearchJoin user={user} />} />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </BrowserRouter>
      </ThemeProvider>
    </GoogleOAuthProvider>
  );
}

export default App;
