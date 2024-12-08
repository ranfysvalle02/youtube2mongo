import os    
import time    
from dotenv import load_dotenv    
from pymongo import MongoClient    
from pymongo.operations import SearchIndexModel    
import scrapetube    
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound, VideoUnavailable    
from langchain.text_splitter import RecursiveCharacterTextSplitter    
from langchain_community.document_loaders.youtube import YoutubeLoader    
from langchain_mongodb import MongoDBAtlasVectorSearch  
from langchain_ollama import OllamaEmbeddings    
import ray    
  
load_dotenv()    
ray.init()  
  
VIDEO_LIMIT = 50    
  
def check_transcript_availability(video_id):    
    """    
    Check if a YouTube video has transcripts available.    
    """    
    try:    
        YouTubeTranscriptApi.list_transcripts(video_id)    
        return True    
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):    
        return False    
    except Exception as e:  
        print(f"An unexpected error occurred while checking transcripts for video {video_id}: {e}")  
        return False  
    
def get_videos(config):    
    """    
    Retrieve videos based on the provided configuration.    
    """    
    try:  
        if config["TYPE"] == "CHANNEL":    
            return list(scrapetube.get_channel(config["ID"]))    
        elif config["TYPE"] == "SEARCH":    
            return list(scrapetube.get_search(config["ID"]))    
        elif config["TYPE"] == "PLAYLIST":    
            return list(scrapetube.get_playlist(config["ID"]))    
        else:    
            raise ValueError("Invalid CONFIG TYPE")    
    except Exception as e:  
        print(f"An error occurred while retrieving videos: {e}")  
        return []  
    
def filter_videos_with_transcripts(videos, video_limit):    
    """    
    Filter videos that have transcripts available.    
    """    
    return [    
        video["videoId"]    
        for video in videos[:video_limit]    
        if check_transcript_availability(video["videoId"])    
    ]    
    
def ensure_search_index(collection, index_name):    
    """    
    Ensure that the specified search index exists.    
    If not, create it and wait until it's ready.    
    """    
    try:  
        indexes = list(collection.list_search_indexes())    
        exists = any(index["name"] == index_name for index in indexes)    
    
        if not exists:    
            print(f"Search index '{index_name}' does not exist. Creating...")    
            search_index_model = SearchIndexModel(    
                definition={    
                    "fields": [    
                        {    
                            "type": "vector",    
                            "numDimensions": 768,    
                            "path": "embedding",    
                            "similarity": "cosine"    
                        },  
                        {  
                            "type": "filter",  
                            "path": "config"  
                        }    
                    ]    
                },    
                name=index_name,    
                type="vectorSearch",    
            )    
            collection.create_search_index(model=search_index_model)    
            # Insert pause to allow index to be created    
            time.sleep(20)    
            print(f"Search index '{index_name}' created successfully.")    
        else:    
            print(f"Search index '{index_name}' already exists.")    
    except Exception as e:  
        print(f"An error occurred while ensuring the search index: {e}")  
    
@ray.remote    
def process_single_video_transcript(video_id, CONFIG):    
    import time    
    from pymongo import MongoClient    
    from langchain.text_splitter import RecursiveCharacterTextSplitter    
    from langchain_community.document_loaders.youtube import YoutubeLoader    
    from langchain_mongodb import MongoDBAtlasVectorSearch    
    from langchain_ollama import OllamaEmbeddings    
      
    try:  
        # Initialize embeddings model    
        embeddings = OllamaEmbeddings(model="nomic-embed-text")    
        
        # MongoDB setup    
        client = MongoClient(os.getenv("MONGODB_URI"))    
        db_name = os.getenv("MONGODB_DATABASE", "default_db")    
        collection_name = os.getenv("MONGODB_COLLECTION", "yt-transcripts")    
        index_name = os.getenv("MONGODB_INDEX_NAME", "vector_index")    
        db = client[db_name]    
        collection = db[collection_name]    
      
        # Check if transcript is available  
        if not check_transcript_availability(video_id):  
            print(f"No transcript available for video {video_id}. Skipping.")  
            return  
        
        # Load the YouTube transcript    
        loader = YoutubeLoader(video_id=video_id)    
        documents = loader.load()    
      
        if not documents:  
            print(f"No documents loaded for video {video_id}. Skipping.")  
            return  
        
        # Split the transcript into chunks    
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=2500, chunk_overlap=250)    
        docs = text_splitter.split_documents(documents)    
        
        # Add metadata    
        for doc in docs:    
            doc.metadata["timestamp"] = time.time()    
            doc.metadata["timestamp_str"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(doc.metadata["timestamp"]))    
            doc.metadata["config"] = str(CONFIG["TYPE"] + ":" + CONFIG["ID"])    
        
        # Delete previous transcripts for this video_id    
        collection.delete_many({"source": video_id})    
        
        # Create vector store from documents    
        vector_store = MongoDBAtlasVectorSearch.from_documents(    
            docs,    
            embeddings,    
            collection=collection,    
            index_name=index_name    
        )    
        
        # Ensure the search index is ready    
        ensure_search_index(collection, index_name)    
          
        print(f"Successfully processed video {video_id}")  
    except Exception as e:  
        print(f"An error occurred while processing video {video_id}: {e}")  
    
def process_video_transcripts(video_ids, embeddings, collection, index_name, CONFIG):    
    """    
    Process the transcripts of each video and store them in the vector store.    
    """    
    for video_id in video_ids:    
        try:  
            # Load the YouTube transcript    
            loader = YoutubeLoader(video_id=video_id, language="en")    
            documents = loader.load()    
        
            if not documents:  
                print(f"No documents loaded for video {video_id}. Skipping.")  
                continue  
        
            # Split the transcript into chunks    
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=2500, chunk_overlap=250)    
            docs = text_splitter.split_documents(documents)    
        
            # Add 'video_id' to metadata    
            for doc in docs:    
                # add timestamp to metadata  
                doc.metadata["timestamp"] = time.time()  
                doc.metadata["timestamp_str"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(doc.metadata["timestamp"]))  
                doc.metadata["config"] = str(CONFIG["TYPE"]+":"+CONFIG["ID"])  
        
            # Delete previous transcripts for this video_id    
            collection.delete_many({"source": video_id})    
        
            # Create vector store from documents    
            vector_store = MongoDBAtlasVectorSearch.from_documents(    
                docs,    
                embeddings,    
                collection=collection,    
                index_name=index_name    
            )    
        
            # Ensure the search index is ready    
            ensure_search_index(collection, index_name)    
              
            print(f"Successfully processed video {video_id}")  
        except Exception as e:  
            print(f"An error occurred while processing video {video_id}: {e}")  
    
def query_vector_store(embeddings, collection, index_name, query, pre_filter=None):    
    """    
    Query the vector store with a given query.    
    """    
    try:  
        vector_store = MongoDBAtlasVectorSearch(    
            embedding=embeddings,    
            collection=collection,    
            index_name=index_name    
        )    
        
        if pre_filter is not None:  
            docs = vector_store.similarity_search(query=query, pre_filter=pre_filter)    
            return docs  
        else:  
            docs = vector_store.similarity_search(query)    
            return docs  
    except Exception as e:  
        print(f"An error occurred during querying the vector store: {e}")  
        return []  
    
def main():      
    # Configuration      
    CONFIG = {      
        "TYPE": "CHANNEL",      
        "ID": "UCCezIgC97PvUuR4_gbFUs5g"      
    }      
    # Uncomment one of the following configurations as needed      
    # CONFIG = {      
    #     "TYPE": "SEARCH",      
    #     "ID": "mongodb atlas vector search + genai"      
    # }      
    # CONFIG = {      
    #     "TYPE": "PLAYLIST",      
    #     "ID": "PL-osiE80TeTt2d9bfVyTiXJA-UTHn6WwU"      
    # }      
    try:  
        # Initialize embeddings model      
        embeddings = OllamaEmbeddings(model="nomic-embed-text")      
        # MongoDB setup      
        client = MongoClient(os.getenv("MONGODB_URI"))      
        db_name = os.getenv("MONGODB_DATABASE", "default_db")      
        collection_name = os.getenv("MONGODB_COLLECTION", "yt-transcripts")      
        index_name = os.getenv("MONGODB_INDEX_NAME", "vector_index")      
        db = client[db_name]      
        collection = db[collection_name]            
        # Retrieve videos based on configuration      
        videos = get_videos(CONFIG)      
        if not videos:  
            print("No videos retrieved. Exiting.")  
            return  
        print(f"Total videos found: {len(videos)}.")      
            
        # Check if any of these videos already exist as 'sources' in the vector store with the same configuration      
        existing_sources = [doc["source"] for doc in collection.find({"config": str(CONFIG["TYPE"] + ":" + CONFIG["ID"])})]      
        print(f"Already existing sources: {existing_sources}")      
            
        # Remove these from the videos list      
        videos = [video for video in videos if video["videoId"] not in existing_sources]      
        print(f"Remaining videos after excluding existing sources: {len(videos)}")      
            
        # Filter videos with available transcripts      
        transcript_available_videos = filter_videos_with_transcripts(videos, VIDEO_LIMIT)      
        print(f"{len(transcript_available_videos)} videos have transcripts available.")      
        
        # Limit to VIDEO_LIMIT videos      
        videos_to_process = transcript_available_videos[:VIDEO_LIMIT]    
        print(f"Processing the following {len(videos_to_process)} videos:")    
        for vid in videos_to_process:    
            print(f"- {vid}")    
        
        if not videos_to_process:  
            print("No videos to process. Exiting.")  
            return  
        
        # Process video transcripts and store in vector store in parallel using Ray      
        tasks = []      
        for video_id in videos_to_process:      
            task = process_single_video_transcript.remote(video_id, CONFIG)      
            tasks.append(task)      
            
        # Wait for all tasks to complete      
        ray.get(tasks)      
        
        print(f"Completed processing {len(videos_to_process)} videos.")    
               
        # Query the vector store      
        query = "anything"    
        pre_filter = {"config": {"$eq": str(CONFIG["TYPE"] + ":" + CONFIG["ID"])}}    
        docs = query_vector_store(embeddings, collection, index_name, query, pre_filter=pre_filter)      
        print(f"Number of documents retrieved: {len(docs)}")    
        print("Sample documents:")    
        for doc in docs[:5]:  # Show a sample of retrieved documents    
            print(str(doc)[:100])    
        # Close MongoDB connection      
        client.close()  
    except Exception as e:  
        print(f"An error occurred in the main function: {e}")  
    
if __name__ == "__main__":    
    main()    
