import React, { useState, useEffect } from "react";
import { checkVerificationStatus, resendVerificationEmail } from "../../utils/api";
import "./VerificationBanner.css";

const VerificationBanner = ({ user }) => {
  const [isVerified, setIsVerified] = useState(true); // Start with true to avoid flashing
  const [isLoading, setIsLoading] = useState(true);
  const [isResending, setIsResending] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const checkStatus = async () => {
      if (user) {
        try {
          const response = await checkVerificationStatus();
          if (response.ok) {
            setIsVerified(response.verified);
          } else {
            console.error("Failed to check verification status:", response.error);
            // If we can't check, assume verified to avoid showing incorrect info
            setIsVerified(true);
          }
        } catch (error) {
          console.error("Error checking verification:", error);
          setIsVerified(true);
        }
      }
      setIsLoading(false);
    };

    checkStatus();
  }, [user]);

  const handleResendVerification = async () => {
    setIsResending(true);
    setMessage("");

    try {
      const response = await resendVerificationEmail();
      if (response.ok) {
        setMessage("Verification email sent! Please check your inbox.");
      } else {
        setMessage("Failed to send verification email. Please try again.");
      }
    } catch (error) {
      console.error("Error resending verification:", error);
      setMessage("Failed to send verification email. Please try again.");
    } finally {
      setIsResending(false);
      
      // Clear message after 5 seconds
      setTimeout(() => setMessage(""), 5000);
    }
  };

  // Don't show anything while loading or if user is verified
  if (isLoading || isVerified) {
    return null;
  }

  return (
    <div className="verification-banner">
      <div className="verification-content">
        <div className="verification-message">
          <strong>Email verification required</strong>
          <p>Please verify your email address to access all features.</p>
        </div>
        <div className="verification-actions">
          <button
            onClick={handleResendVerification}
            disabled={isResending}
            className="verification-button"
          >
            {isResending ? "Sending..." : "Resend verification email"}
          </button>
        </div>
      </div>
      {message && (
        <div className={`verification-feedback ${message.includes("Failed") ? "error" : "success"}`}>
          {message}
        </div>
      )}
    </div>
  );
};

export default VerificationBanner;
