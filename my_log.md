## Solve IT:
## Problem google give only 1hr long key

Here is exactly how I would implement the 30-day session token logic:                                                                                 
  ──────                                                                                                                                                
  ### Step 1: Add a Secret Key to  .env                                                                                                                 
                                                                                                                                                        
  We will add a secure secret key to sign your custom tokens.                                                                                           
                                                                                                                                                        
    JWT_SECRET=your-random-super-secure-secret-key                                                                                                      
  ──────                                                                                                                                                
  ### Step 2: Modify the  /callback  Endpoint (in  mcp_server.py )                                                                                      
                                                                                                                                                        
  Instead of displaying Google's short-lived token, our server will verify the Google login, create a custom payload, sign it with our  JWT_SECRET , and
  set it to expire in 30 days.                                                                                                                          
                                                                                                                                                        
    # Create a custom 30-day token after Google successfully authenticates the user                                                                     
    google_user_info = await validator.verify_google_token_only(id_token) # Extract user profile                                                        
                                                                                                                                                        
    # Calculate 30 days expiration                                                                                                                      
    expire = int(time.time()) + (30 * 24 * 60 * 60)                                                                                                     
                                                                                                                                                        
    payload = {                                                                                                                                         
        "sub": google_user_info["sub"],                                                                                                                 
        "email": google_user_info["email"],                                                                                                             
        "name": google_user_info.get("name", "User"),                                                                                                   
        "exp": expire,                                                                                                                                  
        "iss": "graphmind.local"                                                                                                                        
    }                                                                                                                                                   
                                                                                                                                                        
    # Sign with our local secret key                                                                                                                    
    custom_30_day_token = jwt.encode(payload, os.getenv("JWT_SECRET"), algorithm="HS256")                                                               
  ──────                                                                                                                                                
  ### Step 3: Update  TokenValidator  to accept the 30-day token                                                                                        
                                                                                                                                                        
  We modify the token verification middleware to check if the incoming token was signed by our server's  JWT_SECRET :                                   
                                                                                                                                                        
    async def verify_token(self, token: str) -> dict:                                                                                                   
        # 1. Fallback check for static API key                                                                                                          
        static_key = os.getenv("GRAPHMIND_API_KEY")                                                                                                     
        if static_key and token == static_key:                                                                                                          
            return {"email": "admin@graphmind.local", "name": "Admin User"}                                                                             
                                                                                                                                                        
        # 2. Check if it is a valid 30-day token issued by our server                                                                                   
        try:                                                                                                                                            
            jwt_secret = os.getenv("JWT_SECRET")                                                                                                        
            if jwt_secret:                                                                                                                              
                return jwt.decode(                                                                                                                      
                    token,                                                                                                                              
                    jwt_secret,                                                                                                                         
                    algorithms=["HS256"],                                                                                                               
                    issuer="graphmind.local"                                                                                                            
                )                                                                                                                                       
        except JWTError:                                                                                                                                
            # If HS256 validation fails, fall through to check if it's a raw Google OIDC token                                                          
            pass                                                                                                                                        
                                                                                                                                                        
        # 3. Standard Google OIDC verification (fallback)                                                                                               
        # ... (existing Google cert verification code) ...                                                                                              
  ──────                                                                                                                                                
  ### 🌟 How it will feel for users                                                                                                                     
                                                                                                                                                        
  1. They log in via Google in their browser.                                                                                                           
  2. The browser displays a success page showing a 30-day token.                                                                                        
  3. They paste it once into their  ~/.claude.json .                                                                                                    
  4. They don't have to sign in again for the next 30 days! 



## Provide Citations in MCP