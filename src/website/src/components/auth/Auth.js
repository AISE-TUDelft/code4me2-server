import React, { useState } from "react";
import Login from "./Login";
import Signup from "./Signup";
import PasswordCreationModal from "../common/PasswordCreationModal";
import ThemeToggle from "../common/ThemeToggle";
import { handleGoogleAuth } from "../../utils/auth";
import "./Auth.css";
import { createUser } from "../../utils/api";

const Auth = ({ onAuthenticated }) => {
  const [isLogin, setIsLogin] = useState(true);
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

      await new Promise((resolve) => setTimeout(resolve, 800));

      // Pass the user data and session token to the parent component
      onAuthenticated({
        user: userData.user || userData,
        sessionToken: userData.sessionToken,
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

      await new Promise((resolve) => setTimeout(resolve, 800));

      onAuthenticated({
        user: userData.user || userData,
        sessionToken: userData.sessionToken,
      });
    } catch (error) {
      console.error("Signup error:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleGoogleUser = async (googleUser) => {
    try {
      await handleGoogleAuth(
        googleUser,
        (user) => {
          handleLogin(user);
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

        // Pass the user data and session token to the parent component
        onAuthenticated({
          user: { name, email },
          sessionToken: response.sessionToken,
        });
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
