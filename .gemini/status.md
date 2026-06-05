  ### ⏳ Running in the Background                                                                                                                      
                                                                                                                                                        
  The crawler will take about 50 minutes to complete the remaining ~2,000 pages (due to the built-in 1.5s rate-limit delay to be nice to the wiki       
  server).                                                                                                                                              
                                                                                                                                                        
  I will let it run in the background. Once it completes:                                                                                               
                                                                                                                                                        
  1. We can rerun  python Crawler/cleaner.py  to clean all 3,585 entries.                                                                               
  2. Re-index the vector store and Neo4j graph so the chatbot has access to all the newly scraped knowledge. 