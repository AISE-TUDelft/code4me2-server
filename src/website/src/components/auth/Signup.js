import React, { useState } from "react";
import { GoogleLogin } from "@react-oauth/google";
import "./Auth.css";
import Modal from "../common/Modal";
import { initGoogleOAuth } from "../../utils/auth";
import { createUser } from "../../utils/api";

const Signup = ({ onSwitchToLogin, onSignup, onGoogleAuth }) => {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [modal, setModal] = useState({
    isOpen: false,
    title: "",
    message: "",
    type: "info",
  });
  const [isSubmitting, setIsSubmitting] = useState(false);

  const showModal = (title, message, type) => {
    setModal({ isOpen: true, title, message, type });
  };

  const closeModal = () => {
    setModal({ ...modal, isOpen: false });
    
    // Note: We no longer automatically switch to login on success modal close
    // because we now auto-login the user after successful signup
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setIsSubmitting(true);

    if (!name || !email || !password || !confirmPassword) {
      setError("Please fill in all fields");
      setIsSubmitting(false);
      return;
    }

    if (password !== confirmPassword) {
      setError("Passwords do not match");
      setIsSubmitting(false);
      return;
    }

    // Check if email is valid
    const isValidEmail = (email) => {
      const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
      return emailPattern.test(email);
    };
    if (!isValidEmail(email)) {
      setError("Please enter a valid email address");
      setIsSubmitting(false);
      return;
    }

    // Check if password is strong enough
    const isStrongPassword = (password) => {
      const strongPasswordPattern =
        /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$/;
      return strongPasswordPattern.test(password);
    };
    if (!isStrongPassword(password)) {
      setError(
        "Password must contain at least 8 characters, including uppercase, lowercase letters and numbers",
      );
      setIsSubmitting(false);
      return;
    }

    try {
      console.log("Signup attempt with:", { name, email });

      // Call the createUser function from api.js
      const response = await createUser({ name, email, password });

      if (response.ok) {
        // Show success modal briefly
        showModal("Account Created", "Welcome! Logging you in...", "success");
        
        // Call the parent's signup handler for auto-login
        setTimeout(() => {
          onSignup({
            ...response,
            email,
            password
          });
        }, 1000); // Brief delay to show the success message
      } else {
        // Show error modal
        showModal("Signup Failed", response.error, "error");
      }
    } catch (err) {
      setError("Signup failed. Please try again.");
      console.error("Signup error:", err);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleGoogleSignup = async () => {
    setIsSubmitting(true);
    try {
    } catch (err) {
      setError("Google signup failed. Please try again.");
      console.error("Google signup error:", err);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="auth-container">
      <h2>Sign Up</h2>
      {error && <div className="error-message">{error}</div>}

      {/* Success/Error Modal */}
      <Modal
        isOpen={modal.isOpen}
        onClose={closeModal}
        title={modal.title}
        message={modal.message}
        type={modal.type}
      />

      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label htmlFor="name">Full Name</label>
          <input
            type="text"
            id="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Enter your full name"
            // required
            disabled={isSubmitting}
          />
        </div>

        <div className="form-group">
          <label htmlFor="email">Email</label>
          <input
            type="email"
            id="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Enter your email"
            // required
            disabled={isSubmitting}
          />
        </div>

        <div className="form-group">
          <label htmlFor="password">Password</label>
          <input
            type="password"
            id="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Create a password"
            // required
            disabled={isSubmitting}
          />
        </div>

        <div className="form-group">
          <label htmlFor="confirmPassword">Confirm Password</label>
          <input
            type="password"
            id="confirmPassword"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder="Confirm your password"
            // required
            disabled={isSubmitting}
          />
        </div>

        <button type="submit" className="auth-button" disabled={isSubmitting}>
          {isSubmitting ? "Creating Account..." : "Sign Up"}
        </button>
      </form>

      <div className="auth-switch">
        Already have an account?{" "}
        <button
          onClick={onSwitchToLogin}
          className="switch-button"
          disabled={isSubmitting}
        >
          Login
        </button>
      </div>

      <div className="oauth-section">
        <p>Or sign up with:</p>
        {/*<button */}
        {/*  className="oauth-button"*/}
        {/*  onClick={handleGoogleSignup}*/}
        {/*  disabled={isSubmitting}*/}
        {/*>*/}
        {/*  Google*/}
        {/*</button>*/}
        <GoogleLogin
          onSuccess={async (credentialResponse) => {
            const user = await initGoogleOAuth(credentialResponse);
            console.log("Google signup successful:", user);
            onGoogleAuth(user);
          }}
          onError={() => {
            console.log("Google signup failed");
            setError("Google signup failed. Please try again.");
          }}
        />
      </div>
    </div>
  );
};

export default Signup;
