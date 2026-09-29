import { createContext, useContext } from "react";

/** The account whose session the backend has verified, not just a decoded token. */
export const AuthUserContext = createContext("");
export const useAuthUserId = () => useContext(AuthUserContext);
