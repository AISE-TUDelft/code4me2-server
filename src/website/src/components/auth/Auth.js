import React, { useState } from "react";
import Login from "./Login";
import Signup from "./Signup";
import PasswordCreationModal from "../common/PasswordCreationModal";
import ThemeToggle from "../common/ThemeToggle";
import { handleGoogleAuth } from "../../utils/auth";
import "./Auth.css";
import { createUser } from "../../utils/api";

const Auth = ({ onAuthenticated, initialMode = "login" }) => {
  const [isLogin, setIsLogin] = useState(initialMode === "login");
  const [isLoading, setIsLoading] = useState(false);
  const [passwordModal, setPasswordModal] = useState({
    isOpen: false,
    googleUser: null,
  });

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

  const handleGoogleUser = async (googleUser) => {
    try {
      await handleGoogleAuth(
        googleUser,
        (authResult) => {
          handleLogin(authResult);
        },
        (user) => {
          setPasswordModal({
            isOpen: true,
            googleUser: user,
          });
        },
      );
    } catch (error) {
      console.error("Google auth error:", error);
    }
  };

  const handlePasswordSubmit = async (password) => {
    setIsLoading(true);
    try {
      const { name, email, credential } = passwordModal.googleUser;

      const response = await createUser({
        name,
        email,
        password,
        googleCredential: credential,
      });

      if (response.ok) {
        setPasswordModal({ isOpen: false, googleUser: null });
        
        console.log("Google OAuth user created successfully, now logging in...");
        
        // After successful signup, automatically log the user in using OAuth
        const { authenticateWithOAuth } = await import('../../utils/api');
        const loginResult = await authenticateWithOAuth({
          provider: "google",
          token: credential,
        });
        
        if (loginResult.ok) {
          console.log("Auto-login successful after Google OAuth signup");
          onAuthenticated({
            user: loginResult.user,
            config: loginResult.config,
          });
        } else {
          console.log("Auto-login failed, redirecting to login screen");
          setIsLogin(true);
        }
      } else {
        throw new Error(response.error || "Failed to create account");
      }
    } catch (error) {
      console.error("Password creation error:", error);
      throw error;
    } finally {
      setIsLoading(false);
    }
  };

  const closePasswordModal = () => {
    setPasswordModal({ isOpen: false, googleUser: null });
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

      {/* Password Creation Modal for Google Sign-up */}
      <PasswordCreationModal
        isOpen={passwordModal.isOpen}
        onClose={closePasswordModal}
        googleUser={passwordModal.googleUser}
        onSubmit={handlePasswordSubmit}
      />

      {isLogin ? (
        <Login
          onSwitchToSignup={() => setIsLogin(false)}
          onLogin={handleLogin}
          onGoogleAuth={handleGoogleUser}
        />
      ) : (
        <Signup
          onSwitchToLogin={() => setIsLogin(true)}
          onSignup={handleSignup}
          onGoogleAuth={handleGoogleUser}
        />
      )}
    </div>
  );
};

export default Auth;
