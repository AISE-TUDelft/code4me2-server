import React, { useState, useEffect } from "react";
import Auth from "./components/auth/Auth";
import Dashboard from "./pages/Dashboard";
import "./App.css";
import { GoogleOAuthProvider } from "@react-oauth/google";
import { ThemeProvider } from "./context/ThemeContext";
import { getCurrentUser, logoutUser } from "./utils/api";
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from "react-router-dom";
import Start from "./pages/Start";
function App() {
  // we manage the state for the uer but setting it to null by default
  const [user, setUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  // useEffect to check if the user is already logged in via auth token cookie
  useEffect(() => {
    const checkAuthStatus = async () => {
      try {
        // First check if we have a valid server session
        const response = await getCurrentUser();
        if (response.ok) {
          setUser(response.user);
          // Store user data in localStorage for quick access
          localStorage.setItem("user", JSON.stringify({
            user: response.user,
            config: response.config
          }));
        } else {
          // Server says no valid session, clear everything
          setUser(null);
          localStorage.removeItem("user");
        }
      } catch (error) {
        console.error("Error checking auth status:", error);
        // Network error or server issue, clear local state to be safe
        setUser(null);
        localStorage.removeItem("user");
      } finally {
        setIsLoading(false);
      }
    };

    // Always check server-side auth status first
    checkAuthStatus();
  }, []);

  // handleAuthenticated function to set the user state
  const handleAuthenticated = (userData) => {
    setUser(userData.user);
    localStorage.setItem("user", JSON.stringify(userData));
  };

  // handleLogout function to properly clear user session
  const handleLogout = async () => {
    console.log("Logging out user...");
    
    try {
      // Call the logout API - this will clear server-side session and cookies
      const logoutResponse = await logoutUser();
      console.log("Logout API response:", logoutResponse);
      
      if (!logoutResponse.ok) {
        console.warn("Logout API failed, but continuing with local cleanup");
      }
    } catch (error) {
      console.error("Error during logout API call:", error);
      console.log("Continuing with local cleanup despite API error");
    }
    
    // Clear local state (server handles cookie clearing now)
    setUser(null);
    localStorage.removeItem("user");
    sessionStorage.clear();
    
    console.log("Logout completed successfully");
  };

  // Small helper components for routing
  const ProtectedRoute = ({ children }) => {
    if (!user) return <Navigate to="/login" replace />;
    return children;
  };

  const AuthPage = ({ mode }) => {
    const navigate = useNavigate();
    const onAuth = (userData) => {
      handleAuthenticated(userData);
      navigate("/dashboard", { replace: true });
    };
    return <Auth onAuthenticated={onAuth} initialMode={mode} />;
  };

  const DashboardPage = () => {
    const navigate = useNavigate();
    const onLogoutWrapped = async () => {
      await handleLogout();
      navigate("/", { replace: true });
    };
    return <Dashboard user={user} onLogout={onLogoutWrapped} />;
  };

  // Show loading screen while checking authentication status
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

  // Render application with routing
  return (
    <GoogleOAuthProvider clientId={process.env.REACT_APP_GOOGLE_CLIENT_ID}>
      <ThemeProvider>
        <BrowserRouter>
          <div className="App">
            <Routes>
              <Route
                path="/"
                element={
                  user ? <Navigate to="/dashboard" replace /> : <Start isAuthenticated={!!user} />
                }
              />
              <Route path="/login" element={<AuthPage mode="login" />} />
              <Route path="/signup" element={<AuthPage mode="signup" />} />
              <Route
                path="/dashboard"
                element={
                  <ProtectedRoute>
                    <DashboardPage />
                  </ProtectedRoute>
                }
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </BrowserRouter>
      </ThemeProvider>
    </GoogleOAuthProvider>
  );
}

export default App;
