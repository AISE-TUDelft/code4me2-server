import React, { useState } from "react";
import Login from "./Login";
import Signup from "./Signup";
import ThemeToggle from "../common/ThemeToggle";
import "./Auth.css";

const Auth = ({ onAuthenticated, initialMode = "login" }) => {
  const [isLogin, setIsLogin] = useState(initialMode === "login");
  const [isLoading, setIsLoading] = useState(false);
  // usual login is handled here
  const handleLogin = async (userData) => {
    setIsLoading(true);
    try {
      console.log("Logging in user:", userData);

      // Pass the user data directly from API response
      onAuthenticated({
        user: userData.user || userData,
        config: userData.config,
      });
    } catch (error) {
      console.error("Login error:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleSignup = async (userData) => {
    setIsLoading(true);
    try {
      console.log("Signing up user:", userData);
      
      // userData should contain the response from the createUser API call
      if (userData.ok) {
        console.log("User created successfully, now logging in...");
        
        // After successful signup, automatically log the user in
        // This will use the same credentials they just signed up with
        const loginResult = await import('../../utils/api').then(api => 
          api.authenticateUser({
            email: userData.email,
            password: userData.password
          })
        );
        
        if (loginResult.ok) {
          console.log("Auto-login successful after signup");
          onAuthenticated({
            user: loginResult.user,
            config: loginResult.config,
          });
        } else {
          console.log("Auto-login failed, redirecting to login screen");
          setIsLogin(true);
        }
      } else {
        console.error("Signup failed:", userData.error);
        setIsLogin(true);
      }
    } catch (error) {
      console.error("Signup error:", error);
      setIsLogin(true);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className={`auth-wrapper ${isLoading ? "loading" : ""}`}>
      <ThemeToggle />

      {isLoading && (
        <div className="auth-loading">
          <div className="spinner"></div>
          <p>Please wait...</p>
        </div>
      )}

      {isLogin ? (
        <Login
          onSwitchToSignup={() => setIsLogin(false)}
          onLogin={handleLogin}
        />
      ) : (
        <Signup
          onSwitchToLogin={() => setIsLogin(true)}
          onSignup={handleSignup}
        />
      )}
    </div>
  );
};

export default Auth;
