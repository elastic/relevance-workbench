from flask import Flask, request, jsonify
import os
from elasticsearch import Elasticsearch 

CLOUD_ID = os.environ["CLOUD_ID"]
ES_USER = os.environ['ELASTICSEARCH_USERNAME']
ES_PASSWORD = os.environ['ELASTICSEARCH_PASSWORD']

datasets = {
    "movies": {
        "id": "movies",
        "label": "Movies",
        "index": "search-movies-ml",
        "search_fields": ["title", "overview",  "keywords"],
        "elser_search_fields": ["ml.inference.overview_expanded.predicted_value", "ml.inference.title_expanded.predicted_value^0.5"],
        "result_fields": ["title", "overview"],
        "mapping_fields": {"text": "overview", "title": "title"}
    }
}

app = Flask(__name__)

@app.route("/api/search/<index>")
def route_api_search(index):
    """
    Execute the search
    """
    [query, rrf, type, k, datasetId] = [
        request.args.get('q'),
        request.args.get('rrf', default=False, type=lambda v: v.lower() == 'true'),
        request.args.get('type', default='bm25'),
        request.args.get('k', default=0),
        request.args.get('dataset', default='movies')
    ]
    if type=='elser':
        search_result = run_semantic_search(query, index, **{ 'rrf': rrf, 'k': k, 'dataset': datasetId })
    elif type=='bm25': 
        search_result = run_full_text_search(query, index, **{ 'dataset': datasetId })
    transformed_search_result = transform_search_response(search_result,  datasets[datasetId]['mapping_fields'])
    return jsonify(response=transformed_search_result) 



@app.route("/api/datasets",  methods=['GET'])
def route_api_datasets():
    """
    Return the available datasets
    """
    return datasets


@app.errorhandler(404)
def resource_not_found(e):
    """
    Return a JSON response of the error and the URL that was requested
    """
    return jsonify(error=str(e)), 404

def get_text_expansion_request_body(query, size = 10, **options):
    """
    Generates an ES text expansion search request.
    """
    fields = datasets[options['dataset']]['elser_search_fields']
    result_fields = datasets[options['dataset']]['result_fields']
    text_expansions = []
    boost = 1
   
    for field in fields:

        split_field_descriptor = field.split("^")
        if len(split_field_descriptor) == 2: 
            boost = split_field_descriptor[1]
            field = split_field_descriptor[0]
        te = {"text_expansion": {}}
        te['text_expansion'][field] = {
            "model_text": query,
            "model_id": ".elser_model_1",
            "boost": boost
          }
        text_expansions.append(te)
    return {
        '_source': False,
        'fields': result_fields,
        'size': size,
        'query': {
            "bool": {
                "should": text_expansions
            }
        }        
    }

def get_text_search_request_body(query, size = 10, **options):
    """
    Generates an ES full text search request.
    """
    fields = datasets[options['dataset']]['result_fields']
    search_fields = datasets[options['dataset']]['search_fields']
    return {
        '_source': False,
        'fields': fields,
        'size': size,
        'query': {
            "multi_match" : {
                "query":  query, 
                "fields": search_fields
            }
        }
    }        
    

def execute_search_request(index, body):
    """
    Executes an ES search request and returns the JSON response.
    """
    es = Elasticsearch(
        cloud_id=CLOUD_ID,
        basic_auth=(ES_USER,ES_PASSWORD)
    )
    response = es.search(index=index,query=body["query"], fields=body["fields"], size=body["size"], source=body["_source"])

    return response

def run_full_text_search(query, index, **options):
    """
    Runs a full text search on the given index using the passed query.
    """
    if query is None or query.strip() == '':
        raise Exception('Query cannot be empty')
    body = get_text_search_request_body(query, **options)
    response = execute_search_request(index, body)

    return response['hits']['hits']



def run_semantic_search(query, index, **options):
    """
    Runs a semantic search of the provided query on the target index, and reranks the KNN and BM25 results.
    """
    elser_body = get_text_expansion_request_body(query, **options)
    elser_response_json = execute_search_request(index, elser_body)
    k = int(options['k']) if 'k' in options else 0
    # Rerank hits using RRF
    if options.get('rrf') == True:
        bm25_body = get_text_search_request_body(query, 50, **options)
        bm25_response_json = execute_search_request(index, bm25_body)
        reranked_hits = rerank_hits(elser_response_json['hits']['hits'], bm25_response_json['hits']['hits'], k)
        # Replace final resultset with top 10 reranked hits
        elser_response_json['hits']['hits'] = reranked_hits[:10]

    return elser_response_json['hits']['hits']


def rerank_hits(hits: list, other_hits: list, k: int):
    """
    Reranks hits with RRF. `hits` must contain the main list of documents to rerank. Uses the `_id` attribute of the
    documents to find matching ones in `other_hits`, and `k` as the rank constant.
    Should be done on the Elasticsearch side when supported: https://github.com/elastic/elasticsearch/issues/84324
    """

    i = 0
    for hit in hits:
        i += 1
        # Find the matching ordinal of the doc with the same _id in other_hits
        other_hit_index = find_id_index(hit['_id'], other_hits)
        rrf_score = 1 / (k + i)
        if other_hit_index > 0:
            rrf_score += 1 / (k + other_hit_index)
        hit['_rrf_score'] = rrf_score
        hit['_score'] = f'{round(rrf_score, 5)} ({round(1 / (k + i), 5)} + {round(1 / (k + other_hit_index), 5) if other_hit_index > 0 else 0}), KNN #{i}, BM25: #{other_hit_index}'

    hits.sort(key=lambda hit: hit['_rrf_score'], reverse=True)
    
    return hits


def find_id_index(id: int, hits: list):
    """
    Finds the index of an object in `hits` which has _id == `id`.
    """

    for i, v in enumerate(hits):
        if v['_id'] == id:
            return i + 1
    return 0

def transform_search_response(searchResults, mappingFields):
    for hit in searchResults:
        fields = hit['fields']
        hit['fields'] = {
            'text': fields[mappingFields['text']],
            'title': fields[mappingFields['title']]
        }
    return searchResults

