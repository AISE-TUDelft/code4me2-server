import React, { useState, useEffect } from "react";
import Auth from "./components/auth/Auth";
import Dashboard from "./pages/Dashboard";
import "./App.css";
import { GoogleOAuthProvider } from "@react-oauth/google";
import { ThemeProvider } from "./context/ThemeContext";

function App() {
  // we manage the state for the uer but setting it to null by default
  const [user, setUser] = useState(null);

  // useEffect to check if the user is already logged in
  // if the user is logged in, we set the user state
  // and remove the user from local storage if there is an error
  useEffect(() => {
    const storedUser = localStorage.getItem("user");
    if (storedUser) {
      try {
        setUser(JSON.parse(storedUser));
      } catch (error) {
        console.error("ErrorResponse parsing stored user:", error);
        localStorage.removeItem("user");
      }
    }
  }, []);

  // handleAuthenticated function to set the user state
  const handleAuthenticated = (userData) => {
    setUser(userData);
    localStorage.setItem("user", JSON.stringify(userData));
  };

  // handleLogout function to set the user state to null
  const handleLogout = () => {
    setUser(null);
    localStorage.removeItem("user");
  };

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
