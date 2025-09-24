import React, { useState, useEffect } from "react";
import Auth from "./components/auth/Auth";
import Dashboard from "./pages/Dashboard";
import "./App.css";
import { GoogleOAuthProvider } from "@react-oauth/google";
import { ThemeProvider } from "./context/ThemeContext";
import { getCurrentUser } from "./utils/api";
function App() {
  // we manage the state for the uer but setting it to null by default
  const [user, setUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);

  // useEffect to check if the user is already logged in via auth token cookie
  useEffect(() => {
    const checkAuthStatus = async () => {
      try {
        const response = await getCurrentUser();
        if (response.ok) {
          setUser(response.user);
          // Store user data in localStorage for quick access
          localStorage.setItem("user", JSON.stringify({
            user: response.user,
            config: response.config
          }));
        } else {
          // Clear any stale localStorage data
          localStorage.removeItem("user");
        }
      } catch (error) {
        console.error("Error checking auth status:", error);
        localStorage.removeItem("user");
      } finally {
        setIsLoading(false);
      }
    };

    // Try to get user from current auth token first
    checkAuthStatus();

    // Fallback to localStorage for offline scenarios
    const storedUserData = localStorage.getItem("user");
    if (storedUserData && !user) {
      try {
        const userData = JSON.parse(storedUserData);
        setUser(userData.user);
      } catch (error) {
        console.error("Error parsing stored user:", error);
        localStorage.removeItem("user");
      }
    }
  }, []);

  // handleAuthenticated function to set the user state
  const handleAuthenticated = (userData) => {
    setUser(userData.user);
    localStorage.setItem("user", JSON.stringify(userData));
  };

  // handleLogout function to set the user state to null
  const handleLogout = async () => {
    try {
      // Call the logout API to clear server-side session
      // await logoutUser();
    } catch (error) {
      console.error("Error during logout:", error);
    } finally {
      // Clear local state and storage regardless of API success
      setUser(null);
      localStorage.removeItem("user");
      
      // Force a page refresh to ensure clean state
      window.location.reload();
    }
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

  // when the user is logged in simply render the dashboard, otherwise render the auth component
  return (
    <GoogleOAuthProvider clientId={process.env.REACT_APP_GOOGLE_CLIENT_ID}>
      <ThemeProvider>
        <div className="App">
          {user ? (
            <Dashboard user={user} onLogout={handleLogout} />
          ) : (
            <Auth onAuthenticated={handleAuthenticated} />
          )}
        </div>
      </ThemeProvider>
    </GoogleOAuthProvider>
  );
}

export default App;
